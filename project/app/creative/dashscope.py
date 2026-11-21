"""百炼（DashScope）生图 / 图生图适配器。

为什么单独写一个适配器：百炼的生图接口不是 OpenAI 兼容协议，而是「异步任务」——
先 POST 提交拿 ``task_id``，再轮询任务状态，成功后从返回的 URL 下载图片。
``app.core.llm`` 那套 chat completions 封装覆盖不到这条链路。

设计约束：
1. 配置一律在**调用时**通过 :mod:`app.core.runtime` 读取，不在 import 时固化，
   这样设置页改完 key / 模型后下一次调用立刻生效。
2. 没 key 或 provider 不是 ``dashscope`` 时立刻抛 :class:`DashScopeUnavailable`，
   绝不尝试网络（避免误触发计费）。
3. 任何接口错误、任务失败、轮询超时都翻译成 :class:`DashScopeError`；
   401/403 的消息以固定前缀开头，方便上层引导用户去设置页换 key。
4. key 只出现在请求头里，不进日志、不进异常消息。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.core import runtime

# 百炼默认网关。测试通过注入 httpx.MockTransport 拦截，不会真的访问它。
BASE_URL = "https://dashscope.aliyuncs.com"

NO_KEY_MESSAGE = "未配置百炼 API Key（设置页或 .env 的 DASHSCOPE_API_KEY）"
AUTH_ERROR_PREFIX = "百炼认证失败：API Key 无效或无权限"

TEXT2IMAGE_PATH = "/api/v1/services/aigc/text2image/image-synthesis"
IMAGE2IMAGE_PATH = "/api/v1/services/aigc/image2image/image-synthesis"
TASK_PATH = "/api/v1/tasks/{task_id}"

DEFAULT_IMAGE_MODEL = "wan2.2-t2i-flash"
DEFAULT_EDIT_MODEL = "wan2.5-i2i-preview"

_FAILED_STATES = {"FAILED", "CANCELED"}

# PIL 识别出的格式 → data URL 的 MIME。图生图要求把本地图片内联成 data URL。
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "GIF": "image/gif",
}


class DashScopeUnavailable(RuntimeError):
    """百炼不可用：没有 key，或 ``creative_provider`` 不是 ``dashscope``。"""


class DashScopeError(RuntimeError):
    """百炼接口报错、任务失败或轮询超时。"""


def available() -> bool:
    """当前配置是否真的能调百炼：有 key 且 provider 为 ``dashscope``。

    每次调用都重新读 :mod:`app.core.runtime`，设置页保存后即时生效。
    """
    if runtime.creative_provider() != "dashscope":
        return False
    return bool(runtime.dashscope_key())


def generate(
    *,
    prompt: str,
    out_path: str,
    size: str = "1024*1024",
    model: str | None = None,
    client: httpx.Client | None = None,
    timeout_s: float = 120.0,
    poll_interval_s: float = 2.0,
    max_polls: int = 30,
) -> dict:
    """文生图：提交任务 → 轮询 → 下载到 ``out_path``。

    返回 ``{"path", "url", "model", "task_id", "elapsed_ms"}``。
    """
    return _run(
        submit_path=TEXT2IMAGE_PATH,
        prompt=prompt,
        base_image_path=None,
        out_path=out_path,
        size=size,
        model=model,
        model_field="creative_image_model",
        default_model=DEFAULT_IMAGE_MODEL,
        client=client,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        max_polls=max_polls,
    )


def edit_image(
    *,
    prompt: str,
    base_image_path: str,
    out_path: str,
    size: str = "1024*1024",
    model: str | None = None,
    client: httpx.Client | None = None,
    timeout_s: float = 120.0,
    poll_interval_s: float = 2.0,
    max_polls: int = 30,
) -> dict:
    """图生图：本地图片转 base64 data URL，其余流程与 :func:`generate` 相同。"""
    return _run(
        submit_path=IMAGE2IMAGE_PATH,
        prompt=prompt,
        base_image_path=base_image_path,
        out_path=out_path,
        size=size,
        model=model,
        model_field="creative_edit_model",
        default_model=DEFAULT_EDIT_MODEL,
        client=client,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        max_polls=max_polls,
    )


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _run(
    *,
    submit_path: str,
    prompt: str,
    base_image_path: str | None,
    out_path: str,
    size: str,
    model: str | None,
    model_field: str,
    default_model: str,
    client: httpx.Client | None,
    timeout_s: float,
    poll_interval_s: float,
    max_polls: int,
) -> dict:
    """提交、轮询、下载的公共流程。"""
    if not available():
        raise DashScopeUnavailable(NO_KEY_MESSAGE)

    key = runtime.dashscope_key()
    chosen_model = (model or "").strip() or runtime.value(model_field, default_model)
    started = time.perf_counter()

    payload: dict[str, Any] = {
        "model": chosen_model,
        "input": {"prompt": prompt},
        "parameters": {"size": size, "n": 1},
    }
    if base_image_path is not None:
        payload["input"]["base_image_url"] = _image_data_url(base_image_path)

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout_s)
    try:
        submitted = _post_json(
            http,
            BASE_URL + submit_path,
            payload,
            key=key,
            action="提交生图任务",
            timeout_s=timeout_s,
        )
        task_id = _task_id(submitted)
        if not task_id:
            raise DashScopeError(f"百炼未返回 task_id：{_preview(str(submitted))}")

        image_url = _wait_for_image(
            http,
            task_id=task_id,
            key=key,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            max_polls=max_polls,
        )

        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _download(http, image_url, target, timeout_s=timeout_s)

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return {
            "path": str(target),
            "url": image_url,
            "model": chosen_model,
            "task_id": task_id,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        if owns_client:
            http.close()


def _headers(key: str, *, async_enable: bool) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if async_enable:
        headers["X-DashScope-Async"] = "enable"
    return headers


def _post_json(
    http: httpx.Client,
    url: str,
    payload: dict,
    *,
    key: str,
    action: str,
    timeout_s: float,
) -> dict:
    try:
        response = http.post(
            url,
            json=payload,
            headers=_headers(key, async_enable=True),
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise DashScopeError(
            f"百炼{action}请求失败：{type(exc).__name__}: {exc}"
        ) from exc
    _check_status(response, action)
    return _parse_json(response, action)


def _get_json(
    http: httpx.Client,
    url: str,
    *,
    key: str,
    action: str,
    timeout_s: float,
) -> dict:
    try:
        response = http.get(
            url,
            headers=_headers(key, async_enable=False),
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise DashScopeError(
            f"百炼{action}请求失败：{type(exc).__name__}: {exc}"
        ) from exc
    _check_status(response, action)
    return _parse_json(response, action)


def _check_status(response: httpx.Response, action: str) -> None:
    """非 200 一律报错；401/403 用固定前缀，便于上层提示去换 key。"""
    code = response.status_code
    if code == 200:
        return
    body = _preview(_body_text(response))
    if code in (401, 403):
        raise DashScopeError(f"{AUTH_ERROR_PREFIX}（HTTP {code}）：{body}")
    raise DashScopeError(f"百炼{action}失败：HTTP {code}：{body}")


def _parse_json(response: httpx.Response, action: str) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise DashScopeError(
            f"百炼{action}返回了无法解析的 JSON：{_preview(_body_text(response))}"
        ) from exc
    if not isinstance(data, dict):
        raise DashScopeError(f"百炼{action}返回结构异常：{_preview(str(data))}")
    return data


def _task_id(data: dict) -> str:
    output = data.get("output")
    if isinstance(output, dict):
        task_id = output.get("task_id")
        if task_id:
            return str(task_id)
    task_id = data.get("task_id")
    return str(task_id) if task_id else ""


def _wait_for_image(
    http: httpx.Client,
    *,
    task_id: str,
    key: str,
    timeout_s: float,
    poll_interval_s: float,
    max_polls: int,
) -> str:
    """轮询任务直到成功；超时 / 失败 / 取消都抛 :class:`DashScopeError`。"""
    polls = max(1, int(max_polls))
    url = BASE_URL + TASK_PATH.format(task_id=task_id)

    for attempt in range(polls):
        data = _get_json(
            http,
            url,
            key=key,
            action="查询生图任务",
            timeout_s=timeout_s,
        )
        output = data.get("output")
        output = output if isinstance(output, dict) else {}
        status = str(output.get("task_status") or "").upper()

        if status == "SUCCEEDED":
            image_url = _result_url(output)
            if not image_url:
                raise DashScopeError(f"生图任务成功但未返回图片地址：task_id={task_id}")
            return image_url

        if status in _FAILED_STATES:
            message = str(output.get("message") or data.get("message") or "").strip()
            suffix = f"，message={message}" if message else ""
            raise DashScopeError(
                f"生图任务失败：task_id={task_id}，status={status}{suffix}"
            )

        # PENDING / RUNNING（以及未知状态）都继续等；最后一次不再 sleep。
        if attempt + 1 < polls:
            time.sleep(poll_interval_s)

    raise DashScopeError(f"生图任务超时：task_id={task_id}")


def _result_url(output: dict) -> str:
    results = output.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            return str(first.get("url") or "")
    return ""


def _download(
    http: httpx.Client,
    url: str,
    target: Path,
    *,
    timeout_s: float,
) -> None:
    """下载生成的图片并校验可打开；失败时不留半成品文件。"""
    try:
        response = http.get(url, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise DashScopeError(
            f"下载生成图片失败：{type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise DashScopeError(
            f"下载生成图片失败：HTTP {response.status_code}："
            f"{_preview(_body_text(response))}"
        )
    content = response.content
    if not content:
        raise DashScopeError("下载生成图片失败：响应为空")

    # 原样落盘以保留服务端返回的格式（默认 png），不做重新编码。
    target.write_bytes(content)
    _verify_image(target)


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            mode = image.mode
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都是坏文件
        path.unlink(missing_ok=True)
        raise DashScopeError(
            f"下载的生成图片无法打开：{type(exc).__name__}: {exc}"
        ) from exc
    if width <= 0 or height <= 0 or not mode:
        path.unlink(missing_ok=True)
        raise DashScopeError(f"下载的生成图片尺寸异常：{width}x{height}")


def _image_data_url(path: str) -> str:
    """把本地图片编码成 ``data:image/png;base64,...``，供图生图接口使用。"""
    source = Path(path)
    if not source.is_file():
        raise DashScopeError(f"待编辑图片不存在：{source}")
    try:
        with Image.open(source) as image:
            image_format = (image.format or "").upper()
    except Exception as exc:  # noqa: BLE001 - 非图片文件是明确的调用错误
        raise DashScopeError(
            f"待编辑图片无法打开：{type(exc).__name__}: {exc}"
        ) from exc

    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise DashScopeError(f"读取待编辑图片失败：{exc}") from exc

    mime = _MIME_BY_FORMAT.get(image_format, "image/png")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _body_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:  # noqa: BLE001 - 响应体解不出文本时用空串兜底
        return ""


def _preview(text: str, limit: int = 200) -> str:
    """错误信息里只带响应体前 200 字，避免把整段响应写进消息。"""
    return text if len(text) <= limit else text[:limit] + "…"
