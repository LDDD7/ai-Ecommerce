"""按 `image_feature` 里已有的属性重画 300 张主图，并让「画出来的」对上「写下来的」。

为什么需要这个脚本
------------------
生成器最初是「先定属性、再随手画」：`text_coverage=0.38` 的图，实际文字面积只有 10%，
模特只是一个人台色块。视觉模型看到的东西和 ground truth 对不上，vision suite 必然低分——
不是模型不行，是图和标签不一致。

这个脚本反过来做：读属性 → **按属性画** → 画完量出真实文字占比 → 回写 `features_json`。
画与量在同一个进程里完成，标签就不再是「声称」，而是「实测」。

    python -m tools.rerender_images --db data/store.db [--limit 5] [--no-write]

只重画图与 `image_feature.features_json` 的 `text_coverage`，不动指标与事实表。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.vision.schema import CODE_TO_CN, derive_flags  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIZE = 800
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

# 场景底色：上/下两块，方便画出「有场景」而不是纯色块。
SCENES: dict[str, tuple[str, str]] = {
    "纯白底": ("#FFFFFF", "#F7F7F7"),
    "室内棚拍": ("#EFE7DC", "#D9CBB8"),
    "度假场景": ("#BFE3F2", "#E8D7B0"),
    "户外街拍": ("#DCE3E6", "#B9C2C6"),
    "居家": ("#F3EDE6", "#E2D6C7"),
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold and Path(FONT_BOLD).is_file() else FONT_PATH
    return ImageFont.truetype(path, size)


def _bg(draw: ImageDraw.ImageDraw, scene: str, background: str) -> None:
    """按场景画「一眼能认出来」的背景。

    淡渐变在 detail=low（512px）下会被模型读成「纯白底」，所以每个场景都放一个地标：
    度假=海平线+太阳+沙滩，街拍=楼群+人行道，室内=墙面+地板，居家=沙发+绿植，纯白=纯白。
    """
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
        draw.ellipse([610, 60, 720, 170], fill=(255, 213, 128))          # 太阳
        draw.rectangle([0, 470, SIZE, 560], fill=(126, 186, 204))        # 海
        draw.rectangle([0, 560, SIZE, SIZE], fill=(232, 213, 174))       # 沙滩
        draw.line([(0, 470), (SIZE, 470)], fill=(96, 156, 176), width=4)
    elif key == "户外街拍":
        for i, (x, w, h) in enumerate([(20, 150, 430), (180, 120, 520), (312, 170, 380), (500, 140, 470), (650, 130, 410)]):
            draw.rectangle([x, SIZE - h - 120, x + w, SIZE - 120], fill=(168 + i * 6, 174 + i * 5, 180 + i * 4))
            for wy in range(int(SIZE - h - 100), int(SIZE - 140), 46):
                for wx in range(x + 16, x + w - 20, 38):
                    draw.rectangle([wx, wy, wx + 18, wy + 24], fill=(236, 238, 240))
        draw.rectangle([0, SIZE - 120, SIZE, SIZE], fill=(120, 122, 126))  # 人行道
    elif key == "居家":
        draw.rectangle([0, 560, SIZE, SIZE], fill=(206, 186, 164))         # 地板
        draw.rounded_rectangle([60, 470, 420, 640], radius=26, fill=(150, 166, 158))  # 沙发
        draw.rounded_rectangle([70, 546, 410, 640], radius=18, fill=(172, 186, 178))
        draw.rectangle([640, 470, 700, 640], fill=(186, 170, 150))         # 边几
        draw.ellipse([628, 420, 712, 500], fill=(112, 148, 110))           # 绿植
    else:  # 室内棚拍
        draw.rectangle([0, 600, SIZE, SIZE], fill=(204, 190, 172))         # 地板
        draw.line([(0, 600), (SIZE, 600)], fill=(186, 172, 154), width=3)


def _figure(draw: ImageDraw.ImageDraw, color: str, *, cx: int = 560, with_legs: bool = True) -> None:
    """画一个「看得出是人」的模特：头发、五官、颈、肩、手臂、手、腿、鞋、地面阴影。

    为什么要这么细：视觉模型对「一个圆加一个梯形」不会判成模特，加上头发/五官/鞋之后
    它才会说「图里有模特」。素材诊断的标签必须和肉眼看到的一致，否则评测没有意义。
    """
    s = 0.82
    skin = (226, 190, 164)
    hair = (58, 44, 38)
    head_top, head_h = 150.0, 150.0
    head_w = 62.0

    # 地面阴影：让人「站」在场景里，而不是浮在背景上
    draw.ellipse([cx - 150 * s, 762, cx + 150 * s, 792], fill=(0, 0, 0, 0) if False else (196, 186, 176))

    # 头发（比头略大一圈）与脸
    draw.ellipse([cx - (head_w + 8) * s, head_top - 6, cx + (head_w + 8) * s, head_top + head_h + 10], fill=hair)
    draw.ellipse([cx - head_w * s, head_top + 8, cx + head_w * s, head_top + head_h], fill=skin)
    # 五官：两只眼睛 + 嘴（小图上也能看出是「人」）
    eye_y = head_top + 66 * s
    for dx in (-20, 20):
        draw.ellipse([cx + dx * s - 5, eye_y - 5, cx + dx * s + 5, eye_y + 5], fill=(52, 44, 40))
    draw.arc([cx - 16 * s, eye_y + 14 * s, cx + 16 * s, eye_y + 40 * s], 20, 160, fill=(150, 90, 84), width=4)

    draw.rectangle([cx - 15 * s, head_top + head_h - 8, cx + 15 * s, 340], fill=skin)  # 颈
    # 肩、躯干、裙摆
    draw.polygon(
        [(cx - 132 * s, 330), (cx + 132 * s, 330), (cx + 104 * s, 505), (cx - 104 * s, 505)], fill=color
    )
    draw.polygon(
        [(cx - 104 * s, 503), (cx + 104 * s, 503), (cx + 152 * s, 724), (cx - 152 * s, 724)], fill=color
    )
    # 手臂 + 手（手比袖子浅，容易看出是手臂）
    for sign in (-1, 1):
        x0 = cx + sign * 128 * s
        x1 = cx + sign * 196 * s
        draw.line([(x0, 344), (x1, 540)], fill=color, width=30)
        draw.ellipse([x1 - 13, 528, x1 + 13, 556], fill=skin)
    if with_legs:
        for sign in (-1, 1):
            x = cx + sign * 52 * s
            draw.line([(x, 720), (x + sign * 8, 772)], fill=skin, width=22)
            draw.rounded_rectangle(
                [x - 20 + sign * 8, 772, x + 22 + sign * 8, 788], radius=6, fill=(70, 66, 64)
            )


def _text_boxes(
    draw: ImageDraw.ImageDraw,
    blocks: list[str],
    target_coverage: float,
) -> float:
    """在左侧竖栏画文字块，缩放到「实际面积 ≈ 目标占比」；返回实测占比。"""
    if not blocks:
        return 0.0
    target_area = max(0.01, min(0.6, target_coverage)) * SIZE * SIZE
    col_x, col_w = 34, 372  # 左侧竖栏：不和模特抢位置

    def wrap(text: str, font: ImageFont.FreeTypeFont) -> list[str]:
        lines, cur = [], ""
        for ch in text:
            if draw.textlength(cur + ch, font=font) > col_w - 28:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines[:4]

    def render(scale: float) -> tuple[list[tuple], float]:
        font = _font(max(18, int(46 * scale)), bold=True)
        boxes, area = [], 0.0
        y = 60
        for text in blocks[:3]:
            lines = wrap(text, font)
            line_h = int(font.size * 1.32)
            w = col_w
            h = line_h * len(lines) + 18
            boxes.append((col_x, y, w, h, lines, font))
            area += w * h
            y += h + 16
            if y > 430:  # 文字只占上半部分，下半留给商品
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
    for (x, y, w, h, lines, font) in boxes:
        draw.rectangle([x, y, x + w, y + h], fill=(255, 255, 255), outline=(232, 232, 234))
        for i, line in enumerate(lines):
            draw.text((x + 14, y + 9 + i * int(font.size * 1.32)), line, font=font, fill=(26, 26, 28))
    return area / (SIZE * SIZE)


def _size_chart(draw: ImageDraw.ImageDraw) -> None:
    x0, y0, x1, y1 = 520, 512, 782, 764
    draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255), outline=(90, 90, 95), width=3)
    draw.text((x0 + 16, y0 + 12), "尺码表", font=_font(26, bold=True), fill=(30, 30, 32))
    for i, label in enumerate(("S 胸围84", "M 胸围88", "L 胸围92", "XL 胸围96")):
        y = y0 + 58 + i * 42
        draw.line([(x0 + 12, y - 10), (x1 - 12, y - 10)], fill=(205, 205, 205), width=1)
        draw.text((x0 + 16, y), label, font=_font(20), fill=(60, 60, 64))


def _watermark(img: Image.Image) -> Image.Image:
    """半透明水印：先转 RGBA 叠加再转回 RGB（RGB 图直接 alpha_composite 会报 wrong mode）。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(56, bold=True)
    for i in range(-2, 5):
        d.text((60 + i * 190, 380 + i * 120), "SAMPLE", font=font, fill=(255, 255, 255, 110))
        d.text((60 + i * 190, 380 + i * 120), "SAMPLE", font=font, fill=(120, 120, 120, 60))
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


def _collage(img: Image.Image, scene: str) -> Image.Image:
    """2×2 拼图：四格用不同底色 + 明显分缝，让「拼接」在视觉上成立。"""
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
    # 右下角叠一张「原图」，制造真实拼图的错位感
    canvas.paste(img.resize((half, half)), (half, half))
    return canvas


def render(features: dict[str, Any], *, color: str = "#C9A992") -> tuple[Image.Image, float]:
    """按属性画图；返回 (图, 实测文字占比)。"""
    img = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    scene = str(features.get("scene") or "室内棚拍")
    if features.get("collage"):
        base = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
        d0 = ImageDraw.Draw(base)
        _bg(d0, scene, str(features.get("background") or ""))
        if features.get("model_present"):
            _figure(d0, color)
        img = _collage(base, scene)
        draw = ImageDraw.Draw(img)
    else:
        _bg(draw, scene, str(features.get("background") or ""))
        if features.get("model_present"):
            _figure(draw, color)

    coverage = _text_boxes(draw, list(features.get("text_blocks") or []), float(features.get("text_coverage") or 0.0))
    if features.get("size_chart_present"):
        _size_chart(draw)
    if features.get("watermark"):
        img = _watermark(img)
    return img, coverage


def rerender(db_path: Path, *, limit: int | None = None, write: bool = True) -> dict[str, Any]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT f.image_id, f.sku_id, f.role, f.path, f.features_json, s.color
        FROM image_feature f LEFT JOIN dim_sku s ON s.sku_id = f.sku_id
        ORDER BY f.sku_id
        """
    ).fetchall()
    if limit:
        rows = rows[:limit]

    changed = 0
    out_dir = db_path.parent / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        features = json.loads(row["features_json"])
        img, coverage = render(features, color=_palette_color(features, row["color"]))
        path = ROOT / row["path"] if not Path(row["path"]).is_absolute() else Path(row["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="PNG", optimize=True)
        if write:
            # 标签改成「实测值」：这样评测比的是模型看得见的东西。
            features["text_coverage"] = round(coverage, 4)
            # 问题码也按实测属性重算（生成器原来标的偏松：0.3418 也写了「文字占比偏高」，
            # 而 schema 阈值是 >0.35，两边不一致会让「真值」自相矛盾）。
            codes = derive_flags(
                text_coverage=features["text_coverage"],
                model_present=bool(features.get("model_present")),
                scene=str(features.get("scene") or ""),
                background=str(features.get("background") or ""),
                size_chart_present=bool(features.get("size_chart_present")),
                watermark=bool(features.get("watermark")),
                collage=bool(features.get("collage")),
            )
            features["quality_flags"] = [CODE_TO_CN.get(code, code) for code in codes]
            con.execute(
                "UPDATE image_feature SET features_json = ? WHERE image_id = ?",
                (json.dumps(features, ensure_ascii=False), row["image_id"]),
            )
        changed += 1
    if write:
        con.commit()
    con.close()
    return {"images": changed, "written": write, "dir": str(out_dir)}


def _palette_color(features: dict[str, Any], fallback: str | None) -> str:
    """衣服颜色：优先用属性里的主色，但太浅的颜色在浅底上会「看不见」，换按款式取的实色。"""
    for item in features.get("color_palette") or []:
        text = str(item).strip()
        if len(text) != 7 or not text.startswith("#"):
            continue
        r, g, b = (int(text[i : i + 2], 16) for i in (1, 3, 5))
        if 0.299 * r + 0.587 * g + 0.114 * b < 205:  # 够深才用
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 image_feature 属性重画素材图")
    parser.add_argument("--db", default="data/store.db")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-write", action="store_true", help="只画图，不改 features_json")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_absolute():
        db = ROOT / db
    result = rerender(db, limit=args.limit, write=not args.no_write)
    print(f"重画完成：{result['images']} 张，写回属性={result['written']}，目录 {result['dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
