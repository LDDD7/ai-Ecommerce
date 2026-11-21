"""`app.creative.local` 的离线测试：复制真实素材库到 `.tmp`，逐条验收四类修复。

全程不联网、不写回 `project/data`：数据库与源图复制到 `.tmp/test_creative/<uuid>/`，
`local.PROJECT_ROOT` 指到那个目录，输出也落在里面。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from app.creative import local
from app.creative.schema import CreativeConstraints, CreativeError

PROJECT = Path(__file__).resolve().parents[1]
REAL_DB = PROJECT / "data" / "store.db"
CHART_BOX = (520, 512, 782, 764)
FIXTURE_SKUS = (
    "SKU0001",  # 有尺码表 + 价格块
    "SKU0002",  # 缺尺码表
    "SKU0003",  # 无模特
    "SKU0004",  # 2×2 拼接
    "SKU0006",  # 水印 + 拼接
    "SKU0012",  # 文字超量
    "SKU0014",  # 水印 + 无模特 + 无尺码表
    "SKU0015",  # 纯白底
)


@pytest.fixture(scope="module")
def env() -> Path:
    """真实 store.db 与用到的源图各复制一份到 .tmp/test_creative/<uuid>/。"""
    assert REAL_DB.is_file(), "缺少 data/store.db，测试无法离线准备素材"
    root = PROJECT / ".tmp" / "test_creative" / uuid.uuid4().hex
    (root / "data" / "images").mkdir(parents=True)
    shutil.copy2(REAL_DB, root / "data" / "store.db")
    for sku in FIXTURE_SKUS:
        shutil.copy2(
            PROJECT / "data" / "images" / f"{sku}_main.png",
            root / "data" / "images" / f"{sku}_main.png",
        )

    monkey = pytest.MonkeyPatch()
    monkey.setattr(local, "PROJECT_ROOT", root)
    try:
        yield root
    finally:
        monkey.undo()
        # service 的用例靠 `sys.modules` 注入假的 local；真模块一旦挂到包属性上，
        # `from app.creative import local` 就会绕过注入。测完把真模块摘掉，避免污染后续用例。
        package = sys.modules.get("app.creative")
        if package is not None and getattr(package, "local", None) is local:
            delattr(package, "local")
        sys.modules.pop("app.creative.local", None)


def _db(root: Path) -> str:
    return str(root / "data" / "store.db")


def _render(
    root: Path,
    sku: str,
    issues: list[str],
    draft: str,
    constraints: CreativeConstraints | None = None,
):
    return local.render(
        _db(root),
        sku_id=sku,
        issues=issues,
        constraints=constraints or CreativeConstraints(),
        draft_id=draft,
    )


def _open(root: Path, asset) -> Image.Image:
    return Image.open(root / asset.path).convert("RGB")


def _features(root: Path, sku: str) -> dict:
    conn = sqlite3.connect(str(root / "data" / "store.db"))
    try:
        raw = conn.execute(
            "SELECT features_json FROM image_feature WHERE sku_id=? AND role='main'",
            (sku,),
        ).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


# ---------------------------------------------------------------- 1. TEXT_HEAVY


def test_text_heavy_fits_coverage_budget(env: Path) -> None:
    constraints = CreativeConstraints()
    asset, after, fixes = _render(env, "SKU0012", ["TEXT_HEAVY"], "d-text", constraints)

    assert after["text_coverage"] <= constraints.max_text_coverage + 0.02
    assert "TEXT_HEAVY" not in after["issues_left"]
    assert fixes[0].issue == "TEXT_HEAVY"
    assert fixes[0].status == "done"
    assert (env / asset.path).is_file()


def test_text_heavy_shrinks_for_tighter_budget(env: Path) -> None:
    constraints = CreativeConstraints(max_text_coverage=0.12)
    _asset, after, _fixes = _render(env, "SKU0012", ["TEXT_HEAVY"], "d-text-tight", constraints)

    assert after["text_coverage"] <= 0.12 + 0.02
    assert "TEXT_HEAVY" not in after["issues_left"]


# ---------------------------------------------------------------- 2. NO_SIZE_CHART


def test_size_chart_is_drawn_when_missing(env: Path) -> None:
    asset, after, fixes = _render(env, "SKU0002", ["NO_SIZE_CHART"], "d-chart")

    assert after["size_chart_present"] is True
    assert fixes[0].status == "done"
    chart = _open(env, asset).crop(CHART_BOX).convert("L")
    assert chart.getextrema()[0] < 100, "尺码表区域应当有深色文字，而不是一片白"


def test_existing_size_chart_is_not_drawn_twice(env: Path) -> None:
    assert _features(env, "SKU0001")["size_chart_present"] is True

    asset, after, fixes = _render(env, "SKU0001", ["NO_SIZE_CHART"], "d-chart-exists")

    assert after["size_chart_present"] is True
    assert "未重复" in fixes[0].detail
    # 原图尺码表区域与输出逐像素一致：没有叠第二张表
    source = Image.open(env / "data" / "images" / "SKU0001_main.png").convert("RGB")
    assert ImageChops.difference(_open(env, asset).crop(CHART_BOX), source.crop(CHART_BOX)).getbbox() is None


def test_size_chart_respects_constraint(env: Path) -> None:
    constraints = CreativeConstraints(add_size_chart=False)
    _asset, after, fixes = _render(env, "SKU0002", ["NO_SIZE_CHART"], "d-chart-off", constraints)

    assert after["size_chart_present"] is False
    assert "NO_SIZE_CHART" in after["issues_left"]
    assert fixes[0].status == "manual"


# ---------------------------------------------------------------- 3. PLAIN_WHITE


def test_plain_white_gets_a_scene(env: Path) -> None:
    _asset, after, fixes = _render(env, "SKU0015", ["PLAIN_WHITE"], "d-scene")

    assert after["scene"] != "纯白底"
    assert after["background"] not in ("纯白", "纯色", "")
    assert "PLAIN_WHITE" not in after["issues_left"]
    assert fixes[0].status == "done"


def test_scene_constraint_is_honoured(env: Path) -> None:
    constraints = CreativeConstraints(scene="outdoor")
    asset, after, _fixes = _render(env, "SKU0015", ["PLAIN_WHITE"], "d-scene-outdoor", constraints)

    assert after["scene"] == "户外街拍"
    assert _open(env, asset).getpixel((4, 4)) != (255, 255, 255)


# ---------------------------------------------------------------- 4. WATERMARK


def test_watermark_is_removed(env: Path) -> None:
    asset, after, fixes = _render(env, "SKU0014", ["WATERMARK"], "d-watermark")

    assert after["watermark"] is False
    assert "WATERMARK" not in after["issues_left"]
    assert fixes[0].status == "done"
    assert (env / asset.path).is_file()


# ---------------------------------------------------------------- 5. 人工项


def test_collage_and_no_model_stay_manual(env: Path) -> None:
    for sku, code in (("SKU0004", "COLLAGE"), ("SKU0003", "NO_MODEL")):
        _asset, after, fixes = _render(env, sku, [code], f"d-manual-{code.lower()}")
        assert len(fixes) == 1
        assert fixes[0].issue == code
        assert fixes[0].status == "manual"
        assert fixes[0].action == "manual"
        assert code in after["issues_left"]


# ---------------------------------------------------------------- 6. 资产契约


def test_asset_contract_and_png_file(env: Path) -> None:
    asset, after, _fixes = _render(env, "SKU0012", ["TEXT_HEAVY"], "d-asset")

    assert asset.asset_id == "d-asset-image"
    assert asset.kind == "image"
    assert asset.path == "data/creative/SKU0012/d-asset_main.png"
    assert asset.url == "/api/creative/asset/d-asset"
    assert asset.format == "png"
    assert (asset.width, asset.height) == (800, 800)
    assert after["text_coverage"] is not None

    out = env / asset.path
    assert out.is_file()
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.mode == "RGB"
        assert img.size == (800, 800)


# ---------------------------------------------------------------- 7. 确定性


def test_render_is_pixel_identical_across_runs(env: Path) -> None:
    first, _after1, _fixes1 = _render(env, "SKU0006", ["WATERMARK", "COLLAGE"], "d-det-a")
    second, _after2, _fixes2 = _render(env, "SKU0006", ["WATERMARK", "COLLAGE"], "d-det-b")

    diff = ImageChops.difference(_open(env, first), _open(env, second))
    assert diff.getbbox() is None


# ---------------------------------------------------------------- 8. 未知 SKU


def test_unknown_sku_raises_creative_error(env: Path) -> None:
    with pytest.raises(CreativeError):
        _render(env, "SKU9999", [], "d-missing")


# ---------------------------------------------------------------- 附加：价格块策略


def test_price_tag_only_kept_on_request(env: Path) -> None:
    points = local._selling_points(["通勤显瘦", "限时 ¥148"], keep_price=False)
    kept = local._selling_points(["通勤显瘦", "限时 ¥148"], keep_price=True)
    assert points == ["通勤显瘦"]
    assert "限时 ¥148" in kept

    constraints = CreativeConstraints(keep_price_tag=True)
    _asset, after, fixes = _render(env, "SKU0001", ["TEXT_HEAVY"], "d-price", constraints)
    assert "2 条卖点" in fixes[0].detail
    assert after["text_coverage"] <= constraints.max_text_coverage + 0.02
