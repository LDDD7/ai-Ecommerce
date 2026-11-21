"""本地规则改图（`provider=local`）：把素材诊断给出的问题码，落成一张真正改好的主图。

四条取舍：

1. **同一件衣服重绘，不换主体**。背景、模特、连衣裙按原图属性用同一套几何与主色重绘，只替换
   「文字块 / 尺码表 / 水印」这些贴上去的层；颜色取自 `color_palette` 与 `dim_sku.color`，
   版型、站姿、裙长都不变，因此不会变成另一件衣服。
2. **改完要能过合规**。文字面积按文字块实际占用的像素面积实测，按 ±6% 步长缩放字号，压到
   `constraints.max_text_coverage` 以内；尺码表缺了才补；只有纯白底才加场景；水印不再绘制，
   它覆盖的区域由干净背景与人物重绘填满（不是整块涂灰）。
3. **可复现**。不联网、不用随机数；`scene="auto"` 用 `sku_id` 的稳定下标选场景，同一输入两次
   运行像素级一致。
4. **没把握的活不硬做**。拼图（`COLLAGE`）与无模特（`NO_MODEL`）需要重拍/重排，只能转人工；
   这些码原样留在 `issues_left` 里。

画法复用了 `tools/rerender_images.py`：人物剪影、文字块、场景、尺码表、水印、2×2 拼图保持同一套
几何与配色，因此「改前属性」与「改后属性」才是用同一把尺子量出来的。
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.creative.schema import (
    MANUAL_ONLY,
    CreativeAsset,
    CreativeConstraints,
    CreativeError,
    CreativeFix,
)

# 测试会把 PROJECT_ROOT 指到 `.tmp/...`，所以图片与输出都从这里派生，不写死绝对路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SIZE = 800
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

# 场景底色：上/下两块渐变，外加一个能一眼认出来的地标（与 rerender_images 保持一致）。
SCENES: dict[str, tuple[str, str]] = {
    "纯白底": ("#FFFFFF", "#F7F7F7"),
    "室内棚拍": ("#EFE7DC", "#D9CBB8"),
    "度假场景": ("#BFE3F2", "#E8D7B0"),
    "户外街拍": ("#DCE3E6", "#B9C2C6"),
    "居家": ("#F3EDE6", "#E2D6C7"),
    # `constraints.scene="plain"` 用的素色渐变：不是纯白，也不抢主体。
    "素色背景": ("#F6F2EB", "#E4DCCE"),
}
AUTO_SCENES = ("室内棚拍", "户外街拍", "度假场景")

# 文字竖栏与尺码表的位置（与生成器的几何一致，擦除/重画才对得上）。
TEXT_COL_X = 34
TEXT_COL_W = 372
TEXT_TOP = 60
TEXT_BOTTOM = 430
CHART_BOX = (520, 512, 782, 764)

# 价格块识别：`keep_price_tag=False` 时这些短语不上海报。
PRICE_MARKS = ("¥", "￥", "$", "元", "折", "价")


# ---------------------------------------------------------------- 基础画法


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold and Path(FONT_BOLD).is_file() else FONT_PATH
    return ImageFont.truetype(path, size)


def _bg(draw: ImageDraw.ImageDraw, scene: str, background: str) -> None:
    """按场景画背景。未知场景回退到「室内棚拍」，与生成器一致。"""
    key = scene if scene in SCENES else (background if background in SCENES else "室内棚拍")
    if key == "纯白底":
        draw.rectangle([0, 0, SIZE, SIZE], fill="#FFFFFF")
        return

    top, bottom = SCENES[key]
    for y in range(SIZE):
        t = y / (SIZE - 1)
        r = int(int(top[1:3], 16) * (1 - t) + int(bottom[1:3], 16) * t)
        g = int(int(top[3:5], 16) * (1 - t) + int(bottom[3:5], 16) * t)
        b = int(int(top[5:7], 16) * (1 - t) + int(bottom[5:7], 16) * t)
        draw.line([(0, y), (SIZE, y)], fill=(r, g, b))

    if key == "度假场景":
        draw.ellipse([610, 60, 720, 170], fill=(255, 213, 128))  # 太阳
        draw.rectangle([0, 470, SIZE, 560], fill=(126, 186, 204))  # 海
        draw.rectangle([0, 560, SIZE, SIZE], fill=(232, 213, 174))  # 沙滩
        draw.line([(0, 470), (SIZE, 470)], fill=(96, 156, 176), width=4)
    elif key == "户外街拍":
        for i, (x, w, h) in enumerate(
            [(20, 150, 430), (180, 120, 520), (312, 170, 380), (500, 140, 470), (650, 130, 410)]
        ):
            draw.rectangle(
                [x, SIZE - h - 120, x + w, SIZE - 120],
                fill=(168 + i * 6, 174 + i * 5, 180 + i * 4),
            )
            for wy in range(int(SIZE - h - 100), int(SIZE - 140), 46):
                for wx in range(x + 16, x + w - 20, 38):
                    draw.rectangle([wx, wy, wx + 18, wy + 24], fill=(236, 238, 240))
        draw.rectangle([0, SIZE - 120, SIZE, SIZE], fill=(120, 122, 126))  # 人行道
    elif key == "居家":
        draw.rectangle([0, 560, SIZE, SIZE], fill=(206, 186, 164))  # 地板
        draw.rounded_rectangle([60, 470, 420, 640], radius=26, fill=(150, 166, 158))  # 沙发
        draw.rounded_rectangle([70, 546, 410, 640], radius=18, fill=(172, 186, 178))
        draw.rectangle([640, 470, 700, 640], fill=(186, 170, 150))  # 边几
        draw.ellipse([628, 420, 712, 500], fill=(112, 148, 110))  # 绿植
    elif key == "素色背景":
        draw.rectangle([0, 610, SIZE, SIZE], fill=(214, 204, 190))
        draw.line([(0, 610), (SIZE, 610)], fill=(196, 186, 172), width=3)
    else:  # 室内棚拍
        draw.rectangle([0, 600, SIZE, SIZE], fill=(204, 190, 172))  # 地板
        draw.line([(0, 600), (SIZE, 600)], fill=(186, 172, 154), width=3)


def _soft_shadow(img: Image.Image, cx: int = 560) -> None:
    """脚下柔和阴影：几层由浅到深的椭圆，避免「人物浮在背景上」。"""
    draw = ImageDraw.Draw(img)
    try:
        bg = img.getpixel((cx, 786))
    except (IndexError, ValueError):
        bg = (240, 240, 240)
    tone = (96, 88, 80)
    for rx, ry, alpha in ((152, 14, 0.10), (124, 22, 0.14), (96, 30, 0.18), (68, 38, 0.24)):
        color = tuple(int(bg[i] * (1 - alpha) + tone[i] * alpha) for i in range(3))
        draw.ellipse([cx - rx, 782 - ry, cx + rx, 782 + ry], fill=color)


def _figure(img: Image.Image, color: str, *, cx: int = 560, with_legs: bool = True, shadow: bool = True) -> None:
    """人物剪影：头发、五官、颈、肩、手臂、手、腿、鞋（与生成器同一套坐标）。"""
    draw = ImageDraw.Draw(img)
    s = 0.82
    skin = (226, 190, 164)
    hair = (58, 44, 38)
    head_top, head_h = 150.0, 150.0
    head_w = 62.0

    if shadow:
        draw.ellipse([cx - 150 * s, 762, cx + 150 * s, 792], fill=(196, 186, 176))

    draw.ellipse([cx - (head_w + 8) * s, head_top - 6, cx + (head_w + 8) * s, head_top + head_h + 10], fill=hair)
    draw.ellipse([cx - head_w * s, head_top + 8, cx + head_w * s, head_top + head_h], fill=skin)
    eye_y = head_top + 66 * s
    for dx in (-20, 20):
        draw.ellipse([cx + dx * s - 5, eye_y - 5, cx + dx * s + 5, eye_y + 5], fill=(52, 44, 40))
    draw.arc([cx - 16 * s, eye_y + 14 * s, cx + 16 * s, eye_y + 40 * s], 20, 160, fill=(150, 90, 84), width=4)

    draw.rectangle([cx - 15 * s, head_top + head_h - 8, cx + 15 * s, 340], fill=skin)  # 颈
    draw.polygon(
        [(cx - 132 * s, 330), (cx + 132 * s, 330), (cx + 104 * s, 505), (cx - 104 * s, 505)], fill=color
    )
    draw.polygon(
        [(cx - 104 * s, 503), (cx + 104 * s, 503), (cx + 152 * s, 724), (cx - 152 * s, 724)], fill=color
    )
    for sign in (-1, 1):
        x0 = cx + sign * 128 * s
        x1 = cx + sign * 196 * s
        draw.line([(x0, 344), (x1, 540)], fill=color, width=30)
        draw.ellipse([x1 - 13, 528, x1 + 13, 556], fill=skin)
    if with_legs:
        for sign in (-1, 1):
            x = cx + sign * 52 * s
            draw.line([(x, 720), (x + sign * 8, 772)], fill=skin, width=22)
            draw.rounded_rectangle([x - 20 + sign * 8, 772, x + 22 + sign * 8, 788], radius=6, fill=(70, 66, 64))


def _collage(img: Image.Image) -> Image.Image:
    """2×2 拼图：四格不同底色 + 明显分缝，右下角贴原图（保留「拼接」这一事实）。"""
    canvas = Image.new("RGB", (SIZE, SIZE), (250, 250, 250))
    half = SIZE // 2
    tones = ["#F0E6DA", "#E4EEF2", "#EFE1E6", "#E8EADF"]
    for idx in range(4):
        tile = Image.new("RGB", (half, half), tones[idx])
        d = ImageDraw.Draw(tile)
        d.rectangle([0, 0, half - 1, half - 1], outline=(255, 255, 255), width=6)
        d.ellipse([half // 4, half // 4, half * 3 // 4, half * 3 // 4], fill=(206, 186, 170))
        px, py = (idx % 2) * half, (idx // 2) * half
        canvas.paste(tile, (px, py))
    canvas.paste(img.resize((half, half)), (half, half))
    return canvas


def _size_chart(draw: ImageDraw.ImageDraw) -> None:
    """右下角尺码表：S/M/L/XL 四行，白底描边（与生成器同位置同字号）。"""
    x0, y0, x1, y1 = CHART_BOX
    draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255), outline=(90, 90, 95), width=3)
    draw.text((x0 + 16, y0 + 12), "尺码表", font=_font(26, bold=True), fill=(30, 30, 32))
    for i, label in enumerate(("S 胸围84", "M 胸围88", "L 胸围92", "XL 胸围96")):
        y = y0 + 58 + i * 42
        draw.line([(x0 + 12, y - 10), (x1 - 12, y - 10)], fill=(205, 205, 205), width=1)
        draw.text((x0 + 16, y), label, font=_font(20), fill=(60, 60, 64))


def _watermark(img: Image.Image) -> Image.Image:
    """半透明 SAMPLE 水印：先 RGBA 叠加再转回 RGB（RGB 直接 alpha_composite 会报 wrong mode）。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(56, bold=True)
    for i in range(-2, 5):
        d.text((60 + i * 190, 380 + i * 120), "SAMPLE", font=font, fill=(255, 255, 255, 110))
        d.text((60 + i * 190, 380 + i * 120), "SAMPLE", font=font, fill=(120, 120, 120, 60))
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


# ---------------------------------------------------------------- 文字排版


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines[:4]


def _layout_reduced(blocks: list[str], font_px: int) -> tuple[list[tuple], float, bool]:
    """按给定字号排 1~2 条卖点；返回 (文字块, 面积占比, 是否没压到模特)。"""
    probe = ImageDraw.Draw(Image.new("RGB", (16, 16)))
    font = _font(max(16, int(font_px)), bold=True)
    boxes: list[tuple] = []
    area = 0.0
    y = TEXT_TOP
    fits = True
    for text in blocks[:2]:
        lines = _wrap(probe, text, font, TEXT_COL_W - 28)
        line_h = int(font.size * 1.32)
        h = line_h * len(lines) + 18
        if y + h > TEXT_BOTTOM:  # 再往下就压到模特手臂了
            fits = False
            break
        boxes.append((TEXT_COL_X, y, TEXT_COL_W, h, lines, font))
        area += (TEXT_COL_W + 1) * (h + 1)  # 与 rectangle 的含端点填充一致
        y += h + 16
    return boxes, area / (SIZE * SIZE), fits


def _reduced_layout(blocks: list[str], max_coverage: float) -> tuple[list[tuple], float, int]:
    """字号按可用面积迭代缩放（±6% 步长），返回合规解里面积最大的一组。"""
    if not blocks or max_coverage <= 0.02:
        return [], 0.0, 0
    best: tuple[list[tuple], float, int] | None = None
    size = 46
    for _ in range(48):
        boxes, coverage, fits = _layout_reduced(blocks, size)
        if not fits:
            nxt = int(size * 0.94)
        elif coverage <= max_coverage:
            if best is None or coverage > best[1]:
                best = (boxes, coverage, size)
            if coverage >= max_coverage * 0.9:
                break
            nxt = int(size * 1.06)
        else:
            nxt = int(size * 0.94)
        nxt = max(16, nxt)
        if nxt == size:
            break
        size = nxt
    if best is None:  # 极端参数下兜底：从最小字号往上找第一个合规解
        for size in range(16, 161, 2):
            boxes, coverage, fits = _layout_reduced(blocks, size)
            if fits and coverage <= max_coverage:
                best = (boxes, coverage, size)
                break
    if best is None:
        return [], 0.0, 0
    return best[0], best[1], best[2]


def _original_layout(blocks: list[str], target_coverage: float) -> tuple[list[tuple], float]:
    """复现生成器的原版排版：把文字面积缩放到原图声称的占比，用于「擦除旧文字」。"""
    if not blocks:
        return [], 0.0
    target_area = max(0.01, min(0.6, target_coverage)) * SIZE * SIZE
    probe = ImageDraw.Draw(Image.new("RGB", (16, 16)))

    def render(scale: float) -> tuple[list[tuple], float]:
        font = _font(max(18, int(46 * scale)), bold=True)
        boxes: list[tuple] = []
        area = 0.0
        y = TEXT_TOP
        for text in blocks[:3]:
            lines = _wrap(probe, text, font, TEXT_COL_W - 28)
            line_h = int(font.size * 1.32)
            h = line_h * len(lines) + 18
            boxes.append((TEXT_COL_X, y, TEXT_COL_W, h, lines, font))
            area += (TEXT_COL_W + 1) * (h + 1)  # 实际占用像素（矩形含端点）
            y += h + 16
            if y > TEXT_BOTTOM:
                break
        return boxes, area

    scale = 1.0
    for _ in range(14):
        boxes, area = render(scale)
        if area <= 0:
            break
        if abs(area - target_area) / target_area < 0.06:
            break
        scale = max(0.4, min(2.6, scale * math.sqrt(target_area / area)))
    boxes, area = render(scale)
    return boxes, area / (SIZE * SIZE)


def _draw_text(img: Image.Image, boxes: list[tuple]) -> None:
    draw = ImageDraw.Draw(img)
    for (x, y, w, h, lines, font) in boxes:
        draw.rectangle([x, y, x + w, y + h], fill=(255, 255, 255), outline=(232, 232, 234))
        line_h = int(font.size * 1.32)
        for i, line in enumerate(lines):
            draw.text((x + 14, y + 9 + i * line_h), line, font=font, fill=(26, 26, 28))


# ---------------------------------------------------------------- 属性与选择


def _is_price(text: str) -> bool:
    return any(mark in text for mark in PRICE_MARKS)


def _selling_points(blocks: list[str], keep_price: bool) -> list[str]:
    """留 1~2 条卖点；价格块只在 `keep_price_tag` 时才保留。"""
    texts = [str(b).strip() for b in blocks if str(b).strip()]
    points = [b for b in texts if not _is_price(b)][:2]
    if keep_price:
        for price in [b for b in texts if _is_price(b)]:
            if len(points) < 2:
                points.append(price)
    return points


def _palette_color(features: dict[str, Any], fallback: str | None) -> str:
    """连衣裙主色：优先用够深的主色；太浅就按 SKU 颜色取实色（与生成器一致）。"""
    for item in features.get("color_palette") or []:
        text = str(item).strip()
        if len(text) != 7 or not text.startswith("#"):
            continue
        r, g, b = (int(text[i : i + 2], 16) for i in (1, 3, 5))
        if 0.299 * r + 0.587 * g + 0.114 * b < 205:
            return text
    name = (fallback or "").lower()
    return {
        "black": "#3A3A3C",
        "white": "#B9B2A6",
        "beige": "#C08A6A",
        "red": "#A8412F",
        "blue": "#3E5C7A",
        "green": "#4F7355",
        "pink": "#C4737C",
    }.get(name, "#B4796B")


def _is_plain(scene: str, background: str) -> bool:
    return scene in ("纯白底", "") and background in ("纯色", "纯白", "")


def _pick_scene(constraints: CreativeConstraints, sku_id: str) -> tuple[str, str]:
    """按约束选场景；`auto` 用 sku 编号做稳定下标（同一 SKU 每次一样）。"""
    if constraints.scene == "indoor":
        return "室内棚拍", "场景"
    if constraints.scene == "outdoor":
        return "户外街拍", "场景"
    if constraints.scene == "plain":
        return "素色背景", "渐变"
    tail = sku_id[3:] if len(sku_id) > 3 else ""
    index = int(tail) % len(AUTO_SCENES) if tail.isdigit() else 0
    return AUTO_SCENES[index], "场景"


def _load_source(db_path: str, sku_id: str) -> tuple[dict[str, Any], Path, str | None]:
    """从 image_feature 取主图属性与源图路径；没有图就抛 CreativeError。"""
    db_file = Path(db_path)
    if not db_file.is_file():
        raise CreativeError(f"数据库不存在：{db_path}")
    try:
        conn = sqlite3.connect(str(db_file))
    except sqlite3.Error as exc:  # 库打不开也属于「工单可预期失败」
        raise CreativeError(f"数据库不可用：{db_path}（{exc}）") from exc
    try:
        row = conn.execute(
            "SELECT f.path, f.features_json, s.color"
            " FROM image_feature f LEFT JOIN dim_sku s ON s.sku_id = f.sku_id"
            " WHERE f.sku_id = ? AND f.role = 'main' LIMIT 1",
            (sku_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise CreativeError(f"读取主图失败：{sku_id}（{exc}）") from exc
    finally:
        conn.close()
    if not row:
        raise CreativeError(f"找不到主图：{sku_id}（role=main）")
    path_text, raw, color = row
    try:
        features = json.loads(raw or "{}")
    except json.JSONDecodeError:
        features = {}
    path = Path(str(path_text or ""))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise CreativeError(f"主图文件不存在：{path_text}")
    return features, path, color


# ---------------------------------------------------------------- 拼装


def _render_clean(
    features: dict[str, Any],
    *,
    scene: str,
    background: str,
    color: str,
    soft_shadow: bool,
) -> Image.Image:
    """背景 + 模特 +（可能的）拼图，不带文字、尺码表、水印。"""
    img = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    if features.get("collage"):
        base = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
        draw = ImageDraw.Draw(base)
        _bg(draw, scene, background)
        if features.get("model_present"):
            if soft_shadow:
                _soft_shadow(base)
            _figure(base, color, shadow=not soft_shadow)
        return _collage(base)

    draw = ImageDraw.Draw(img)
    _bg(draw, scene, background)
    if features.get("model_present"):
        if soft_shadow:
            _soft_shadow(img)
        _figure(img, color, shadow=not soft_shadow)
    return img


def _issues_after(
    *,
    coverage: float,
    size_chart: bool,
    scene: str,
    background: str,
    watermark: bool,
    collage: bool,
    model: bool,
    constraints: CreativeConstraints,
    requested: list[str],
) -> list[str]:
    """按「改完的实测属性」重新派生问题码；人工项只要被请求就保留。"""
    left: list[str] = []
    if not size_chart:
        left.append("NO_SIZE_CHART")
    if coverage > constraints.max_text_coverage + 0.02:
        left.append("TEXT_HEAVY")
    if _is_plain(scene, background):
        left.append("PLAIN_WHITE")
    if watermark:
        left.append("WATERMARK")
    if collage:
        left.append("COLLAGE")
    if not model:
        left.append("NO_MODEL")
    for code in requested:
        if code in MANUAL_ONLY and code not in left:
            left.append(code)
    return left


# ---------------------------------------------------------------- 对外入口


def render(
    db_path: str,
    *,
    sku_id: str,
    issues: list[str],
    constraints: CreativeConstraints,
    draft_id: str,
    target: str = "main_image",
) -> tuple[CreativeAsset, dict, list[CreativeFix]]:
    """按 issues 改图，返回 (资产, after 属性字典, 修复清单)。

    `target` 目前只用于区分调用场景（主图/详情图），文件名与画布尺寸仍按主图规范输出。
    """
    features, _source_path, sku_color = _load_source(db_path, sku_id)
    requested = list(dict.fromkeys(str(code) for code in issues))
    color = _palette_color(features, sku_color)

    orig_scene = str(features.get("scene") or "")
    orig_background = str(features.get("background") or "")
    scene_changed = "PLAIN_WHITE" in requested and _is_plain(orig_scene, orig_background)
    if scene_changed:
        scene, background = _pick_scene(constraints, sku_id)
    else:
        scene, background = orig_scene, orig_background

    original_blocks = [str(b) for b in features.get("text_blocks") or [] if str(b).strip()]
    orig_coverage = float(features.get("text_coverage") or 0.0)

    if "TEXT_HEAVY" in requested:
        blocks = _selling_points(features.get("text_blocks") or [], constraints.keep_price_tag)
        boxes, coverage, font_px = _reduced_layout(blocks, constraints.max_text_coverage)
    else:
        blocks = []
        boxes, coverage = _original_layout(original_blocks, orig_coverage)
        font_px = boxes[-1][5].size if boxes else 0

    orig_chart = bool(features.get("size_chart_present"))
    chart = orig_chart or ("NO_SIZE_CHART" in requested and bool(constraints.add_size_chart))

    has_watermark = bool(features.get("watermark"))
    keep_watermark = has_watermark and "WATERMARK" not in requested

    # 干净底：背景 + 模特 + 连衣裙（+ 拼图），没有任何贴上去的文字/尺码表/水印。
    # 水印不重绘，它原本覆盖的像素就由这层背景与人物填满，等价于「用周边内容重绘覆盖」。
    canvas = _render_clean(
        features,
        scene=scene,
        background=orig_background,
        color=color,
        soft_shadow=scene_changed,
    )

    _draw_text(canvas, boxes)
    if chart:
        _size_chart(ImageDraw.Draw(canvas))
    if keep_watermark:
        canvas = _watermark(canvas)

    if constraints.size != SIZE:
        canvas = canvas.resize((constraints.size, constraints.size), Image.LANCZOS)
    canvas = canvas.convert("RGB")

    out_dir = PROJECT_ROOT / "data" / "creative" / sku_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{draft_id}_main.png"
    canvas.save(out_path, format="PNG", optimize=True)

    width, height = canvas.size
    asset = CreativeAsset(
        asset_id=f"{draft_id}-image",
        kind="image",
        path=out_path.relative_to(PROJECT_ROOT).as_posix(),
        url=f"/api/creative/asset/{draft_id}",
        width=width,
        height=height,
        format="png",
    )

    after: dict[str, Any] = {
        "text_coverage": round(coverage, 4),
        "size_chart_present": bool(chart),
        "watermark": bool(keep_watermark),
        "collage": bool(features.get("collage")),
        "model_present": bool(features.get("model_present")),
        "scene": scene,
        "background": background,
        "issues_left": _issues_after(
            coverage=coverage,
            size_chart=chart,
            scene=scene,
            background=background,
            watermark=keep_watermark,
            collage=bool(features.get("collage")),
            model=bool(features.get("model_present")),
            constraints=constraints,
            requested=requested,
        ),
    }

    fixes: list[CreativeFix] = []
    for code in requested:
        if code == "TEXT_HEAVY":
            if coverage <= constraints.max_text_coverage + 0.02:
                detail = (
                    f"保留 {len(blocks)} 条卖点，字号 {font_px}px，"
                    f"文字面积实测 {coverage:.1%}（上限 {constraints.max_text_coverage:.0%}）"
                    if blocks
                    else "原图没有可保留的卖点短语，已清空文字块"
                )
                fixes.append(CreativeFix(issue=code, action="relayout_text", detail=detail, status="done"))
            else:
                fixes.append(
                    CreativeFix(
                        issue=code,
                        action="manual",
                        detail=f"文字面积 {coverage:.1%} 仍超过上限，需人工压缩文案",
                        status="manual",
                    )
                )
        elif code == "NO_SIZE_CHART":
            if orig_chart:
                fixes.append(
                    CreativeFix(
                        issue=code,
                        action="add_size_chart",
                        detail="原图已有尺码表，未重复绘制",
                        status="done",
                    )
                )
            elif chart:
                fixes.append(
                    CreativeFix(
                        issue=code,
                        action="add_size_chart",
                        detail="右下角补画尺码表：S/M/L/XL 胸围 84/88/92/96",
                        status="done",
                    )
                )
            else:
                fixes.append(
                    CreativeFix(
                        issue=code,
                        action="manual",
                        detail="约束要求不加尺码表，需人工确认后处理",
                        status="manual",
                    )
                )
        elif code == "PLAIN_WHITE":
            if scene_changed:
                detail = f"加浅色场景底（{scene}）"
                if features.get("model_present"):
                    detail += "，并在人物脚下加柔和阴影"
                fixes.append(CreativeFix(issue=code, action="add_scene", detail=detail, status="done"))
            else:
                fixes.append(
                    CreativeFix(
                        issue=code,
                        action="add_scene",
                        detail=f"原图场景为「{orig_scene or '未知'}」，不属于纯白底，无需改动",
                        status="done",
                    )
                )
        elif code == "WATERMARK":
            fixes.append(
                CreativeFix(
                    issue=code,
                    action="remove_watermark",
                    detail=(
                        "水印区域用周边背景重绘覆盖，未整块涂灰"
                        if has_watermark
                        else "原图未检测到水印，未做改动"
                    ),
                    status="done",
                )
            )
        elif code == "COLLAGE":
            fixes.append(
                CreativeFix(
                    issue=code,
                    action="manual",
                    detail="拼接图需重新拍摄或重新排版，本工具不会自动拼接",
                    status="manual",
                )
            )
        elif code == "NO_MODEL":
            fixes.append(
                CreativeFix(
                    issue=code,
                    action="manual",
                    detail="缺少模特上身图，需重拍或补拍，无法用现有素材生成",
                    status="manual",
                )
            )
        else:
            fixes.append(
                CreativeFix(
                    issue=code,
                    action="manual",
                    detail="未知问题码，需人工处理",
                    status="manual",
                )
            )

    return asset, after, fixes
