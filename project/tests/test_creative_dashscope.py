"""``app.creative.dashscope`` 的离线测试。

全部通过 ``httpx.MockTransport`` 注入假网络：提交任务、轮询、下载图片都在内存里
完成，不发任何真实请求，也不消耗百炼额度。轮询用的 ``time.sleep`` 也被打桩，
测试不会真的等待。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from PIL import Image

import app.creative as _creative_pkg
from app.core import runtime
from app.creative import dashscope

TASK_ID = "task-abc123"
IMAGE_URL = "https://cdn.example.com/generated.png"
IMAGE_MODEL = "wan2.2-t2i-flash"
EDIT_MODEL = "wan2.5-i2i-preview"
TEST_KEY = "sk-test"


def _png_bytes(size: tuple[int, int] = (64, 64), color: tuple[int, int, int] = (180, 30, 30)) -> bytes:
    """在内存里生成一张真实的 PNG，作为「服务端返回的图片」。"""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _runtime_value(name: str, default: str | None = None) -> str:
    """替代 ``runtime.value``，避免测试依赖真实 .env / 数据库里的配置。"""
    return {
        "creative_image_model": IMAGE_MODEL,
        "creative_edit_model": EDIT_MODEL,
    }.get(name, default or "")


class FakeDashScope:
    """模拟百炼的三条 HTTP 接口：提交任务、查询任务、下载图片。"""

    def __init__(
        self,
        *,
        statuses: tuple[str, ...] = ("PENDING", "SUCCEEDED"),
        image_bytes: bytes | None = None,
        submit_status: int = 200,
        submit_body: dict | None = None,
        task_message: str | None = None,
        download_status: int = 200,
    ) -> None:
        self.statuses = tuple(statuses)
        self.image = image_bytes if image_bytes is not None else _png_bytes()
        self.submit_status = submit_status
        self.submit_body = submit_body if submit_body is not None else {"message": "boom"}
        self.task_message = task_message
        self.download_status = download_status
        self.requests: list[httpx.Request] = []
        self.payloads: list[dict] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.method == "POST":
            self.payloads.append(json.loads(request.content))
            if self.submit_status != 200:
                return httpx.Response(self.submit_status, json=self.submit_body)
            return httpx.Response(
                200,
                json={"output": {"task_id": TASK_ID, "task_status": "PENDING"}},
            )

        if request.method == "GET" and request.url.path.startswith("/api/v1/tasks/"):
            index = min(self.polls, len(self.statuses) - 1)
            status = self.statuses[index]
            self.polls += 1
            output: dict = {"task_status": status}
            if status == "SUCCEEDED":
                output["results"] = [{"url": IMAGE_URL}]
            if self.task_message is not None:
                output["message"] = self.task_message
            return httpx.Response(200, json={"output": output})

        if request.method == "GET" and str(request.url) == IMAGE_URL:
            if self.download_status != 200:
                return httpx.Response(self.download_status, content=b"upstream boom")
            return httpx.Response(200, content=self.image)

        return httpx.Response(
            404,
            json={"message": f"unexpected request: {request.method} {request.url}"},
        )


@pytest.fixture
def workdir() -> Path:
    """工作区内的独立临时目录（本机 %TEMP% 子目录 ACL 异常，见 conftest）。"""
    path = (
        Path(__file__).resolve().parents[1]
        / ".tmp"
        / "test_creative_dashscope"
        / uuid.uuid4().hex
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """打桩 ``time.sleep``：dashscope 里是 ``time.sleep(...)``，改模块属性即可。"""
    recorded: list[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def ready_runtime(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    """provider=dashscope、key=sk-test、模型用固定值。"""
    monkeypatch.setattr(runtime, "creative_provider", lambda: "dashscope")
    monkeypatch.setattr(runtime, "dashscope_key", lambda: TEST_KEY)
    monkeypatch.setattr(runtime, "value", _runtime_value)


@pytest.fixture(scope="module", autouse=True)
def _isolate_real_module() -> None:
    """本模块结束后卸掉真实模块，避免污染后续测试的 ``sys.modules`` 假模块注入。

    ``test_creative_service.py`` 用 ``sys.modules["app.creative.dashscope"]`` 换成
    假模块；但真实模块一旦被本模块 import 过，同时会挂在 ``app.creative`` 包属性
    上，``from app.creative import dashscope`` 会优先命中包属性而绕过假模块。
    本模块按文件名排在其前面执行，因此退出时把包属性与 sys.modules 条目一并清理。
    """
    yield
    if getattr(_creative_pkg, "dashscope", None) is dashscope:
        delattr(_creative_pkg, "dashscope")
    if sys.modules.get("app.creative.dashscope") is dashscope:
        del sys.modules["app.creative.dashscope"]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 可用性 / 不发请求
# ---------------------------------------------------------------------------


def test_missing_key_is_unavailable_and_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    monkeypatch.setattr(runtime, "creative_provider", lambda: "dashscope")
    monkeypatch.setattr(runtime, "dashscope_key", lambda: "")

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被执行
        calls.append(request)
        raise AssertionError("没有 key 时不应该发起任何网络请求")

    out = workdir / "cover.png"
    with _client(handler) as client:
        assert dashscope.available() is False
        with pytest.raises(dashscope.DashScopeUnavailable) as excinfo:
            dashscope.generate(prompt="红色连衣裙", out_path=str(out), client=client)

    assert str(excinfo.value) == dashscope.NO_KEY_MESSAGE
    assert calls == []
    assert not out.exists()


def test_provider_local_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    monkeypatch.setattr(runtime, "creative_provider", lambda: "local")
    monkeypatch.setattr(runtime, "dashscope_key", lambda: TEST_KEY)

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被执行
        calls.append(request)
        raise AssertionError("provider 不是 dashscope 时不应该发起网络请求")

    with _client(handler) as client:
        assert dashscope.available() is False
        with pytest.raises(dashscope.DashScopeUnavailable):
            dashscope.edit_image(
                prompt="换背景",
                base_image_path=str(workdir / "base.png"),
                out_path=str(workdir / "out.png"),
                client=client,
            )

    assert calls == []


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_generate_happy_path(ready_runtime: None, workdir: Path, sleeps: list[float]) -> None:
    fake = FakeDashScope(statuses=("PENDING", "SUCCEEDED"))
    out = workdir / "nested" / "cover.png"

    with _client(fake) as client:
        result = dashscope.generate(
            prompt="一件红色连衣裙，白色背景",
            out_path=str(out),
            client=client,
        )

    # 文件真的落盘，而且是 PIL 能打开、尺寸正确的 PNG。
    assert out.is_file()
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (64, 64)

    assert result["path"] == str(out)
    assert result["url"] == IMAGE_URL
    assert result["task_id"] == TASK_ID
    assert result["model"] == IMAGE_MODEL
    assert isinstance(result["elapsed_ms"], (int, float))

    # 一次 PENDING + 一次 SUCCEEDED；中间只 sleep 一次（已被打桩）。
    assert fake.polls == 2
    assert sleeps == [2.0]

    submit = fake.payloads[0]
    assert submit["input"]["prompt"] == "一件红色连衣裙，白色背景"
    assert submit["parameters"] == {"size": "1024*1024", "n": 1}


def test_failed_task_includes_server_message(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(statuses=("FAILED",), task_message="内容不合规，请修改提示词")

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(
                prompt="违规内容",
                out_path=str(workdir / "cover.png"),
                client=client,
            )

    message = str(excinfo.value)
    assert "内容不合规" in message
    assert TASK_ID in message
    assert "FAILED" in message


def test_poll_timeout_after_max_polls(
    ready_runtime: None, workdir: Path, sleeps: list[float]
) -> None:
    fake = FakeDashScope(statuses=("RUNNING",))

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(
                prompt="慢慢画",
                out_path=str(workdir / "cover.png"),
                client=client,
                max_polls=2,
                poll_interval_s=0.01,
            )

    message = str(excinfo.value)
    assert "超时" in message
    assert f"task_id={TASK_ID}" in message
    assert fake.polls == 2
    assert len(sleeps) == 1


# ---------------------------------------------------------------------------
# 接口错误
# ---------------------------------------------------------------------------


def test_auth_failure_has_fixed_prefix(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(
        submit_status=401,
        submit_body={"message": "Invalid API-key provided."},
    )

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(
                prompt="红色连衣裙",
                out_path=str(workdir / "cover.png"),
                client=client,
            )

    message = str(excinfo.value)
    assert message.startswith("百炼认证失败：API Key 无效或无权限")
    assert "401" in message
    # key 不允许出现在异常消息里。
    assert TEST_KEY not in message


def test_server_error_includes_status_and_body(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(
        submit_status=500,
        submit_body={"message": "internal error"},
    )

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(
                prompt="红色连衣裙",
                out_path=str(workdir / "cover.png"),
                client=client,
            )

    message = str(excinfo.value)
    assert "500" in message
    assert "internal error" in message


# ---------------------------------------------------------------------------
# 图生图 / 模型 / 请求头
# ---------------------------------------------------------------------------


def test_edit_image_sends_base64_data_url(ready_runtime: None, workdir: Path) -> None:
    base = workdir / "base.png"
    base_bytes = _png_bytes(size=(32, 32), color=(10, 120, 200))
    base.write_bytes(base_bytes)
    fake = FakeDashScope(statuses=("SUCCEEDED",))
    out = workdir / "edited.png"

    with _client(fake) as client:
        result = dashscope.edit_image(
            prompt="把背景改成纯白",
            base_image_path=str(base),
            out_path=str(out),
            client=client,
        )

    assert fake.requests[0].url.path == "/api/v1/services/aigc/image2image/image-synthesis"
    submit = fake.payloads[0]
    data_url = submit["input"]["base_image_url"]
    prefix = "data:image/png;base64,"
    assert data_url.startswith(prefix)
    assert base64.b64decode(data_url[len(prefix) :]) == base_bytes

    # 不传 model 时用 runtime 的图生图默认模型。
    assert submit["model"] == EDIT_MODEL
    assert result["model"] == EDIT_MODEL
    assert out.is_file()


def test_custom_model_is_written_to_payload(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(statuses=("SUCCEEDED",))

    with _client(fake) as client:
        result = dashscope.generate(
            prompt="红色连衣裙",
            out_path=str(workdir / "cover.png"),
            model="wan-custom-v9",
            client=client,
        )

    assert fake.payloads[0]["model"] == "wan-custom-v9"
    assert result["model"] == "wan-custom-v9"


def test_default_image_model_comes_from_runtime(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(statuses=("SUCCEEDED",))

    with _client(fake) as client:
        dashscope.generate(
            prompt="红色连衣裙",
            out_path=str(workdir / "cover.png"),
            client=client,
        )

    assert fake.payloads[0]["model"] == IMAGE_MODEL


def test_request_headers_include_async_and_bearer(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(statuses=("SUCCEEDED",))

    with _client(fake) as client:
        dashscope.generate(
            prompt="红色连衣裙",
            out_path=str(workdir / "cover.png"),
            client=client,
        )

    submit = fake.requests[0]
    assert submit.url.host == "dashscope.aliyuncs.com"
    assert submit.url.path == "/api/v1/services/aigc/text2image/image-synthesis"
    assert submit.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert submit.headers["X-DashScope-Async"] == "enable"
    assert submit.headers["Content-Type"] == "application/json"

    task_requests = [
        request for request in fake.requests if request.url.path.startswith("/api/v1/tasks/")
    ]
    assert task_requests
    assert task_requests[0].url.path == f"/api/v1/tasks/{TASK_ID}"
    assert task_requests[0].headers["Authorization"] == f"Bearer {TEST_KEY}"


# ---------------------------------------------------------------------------
# 下载失败
# ---------------------------------------------------------------------------


def test_download_failure_raises_and_leaves_no_file(
    ready_runtime: None, workdir: Path
) -> None:
    fake = FakeDashScope(statuses=("SUCCEEDED",), download_status=503)
    out = workdir / "cover.png"

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(prompt="红色连衣裙", out_path=str(out), client=client)

    message = str(excinfo.value)
    assert "下载" in message
    assert "503" in message
    assert not out.exists()


def test_non_image_download_is_rejected(ready_runtime: None, workdir: Path) -> None:
    fake = FakeDashScope(statuses=("SUCCEEDED",), image_bytes=b"<html>not an image</html>")
    out = workdir / "cover.png"

    with _client(fake) as client:
        with pytest.raises(dashscope.DashScopeError) as excinfo:
            dashscope.generate(prompt="红色连衣裙", out_path=str(out), client=client)

    assert "无法打开" in str(excinfo.value)
    # 坏文件不允许留在磁盘上冒充成品。
    assert not out.exists()
