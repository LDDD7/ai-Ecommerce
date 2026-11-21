"""数据接入的测试：能不能认出来、认错了能不能挡、写进去会不会重复。

样例文件在 `project/samples/`，就是四种真实导出格式的最小版本。
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ingest import quality, service, store
from app.ingest.canonical import DATASETS, parse_date, parse_money
from app.ingest.detect import detect
from app.ingest.loaders import read_table

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _table(name: str):
    return read_table((SAMPLES / name).read_bytes(), name)


# ---------- 解析：日期与金额 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-11-17", "2026-11-17"),
        ("2026/11/17", "2026-11-17"),
        ("2026年11月17日", "2026-11-17"),
        ("20261117", "2026-11-17"),
        ("2026.11.17", "2026-11-17"),
        ("", None),
        ("不是日期", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("¥1,299.00", 1299.0),
        ("1299", 1299.0),
        ("(120.5)", -120.5),
        ("-8.8", -8.8),
        ("", None),
        ("--", None),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


# ---------- 识别：四份样例各归各位 ----------


def test_detect_taobao_orders():
    detection = detect(_table("taobao_orders.csv"))
    assert detection.dataset == "orders"
    mapping = detection.mapping
    assert mapping["order_id"] == "订单编号"
    assert mapping["dt"] == "下单时间"
    assert mapping["pay_amount"] == "买家实付金额"
    assert not detection.missing


def test_detect_douyin_traffic_and_keeps_direction():
    detection = detect(_table("douyin_traffic.csv"))
    assert detection.dataset == "traffic"
    # 曝光与点击不能被「都是整数」搞反：抖音的列叫「曝光人数/点击人数」
    assert detection.mapping["impressions"] == "曝光人数"
    assert detection.mapping["clicks"] == "点击人数"
    assert detection.mapping["sku_id"] == "商品ID"


def test_detect_erp_refunds():
    detection = detect(_table("erp_refunds.csv"))
    assert detection.dataset == "refunds"
    assert detection.mapping["refund_id"] == "售后单号"
    assert detection.mapping["reason"] == "售后原因"


def test_detect_shopify_products_with_english_headers():
    detection = detect(_table("shopify_products.csv"))
    assert detection.dataset == "products"
    assert detection.mapping["sku_id"] == "sku"
    assert detection.mapping["title"] == "title"


def test_detect_forced_dataset_still_reports_missing():
    detection = detect(_table("douyin_traffic.csv"), dataset="orders")
    assert detection.dataset == "orders"
    assert "订单号" in detection.missing


# ---------- 质量闸 ----------


def test_quality_passes_clean_file():
    table = _table("taobao_orders.csv")
    detection = detect(table)
    outcome = quality.check("orders", table, detection.mapping)
    assert outcome.verdict == "pass"
    assert outcome.rows_ok == 15
    assert outcome.identity_failed == 0
    assert outcome.date_min == "2026-11-10"
    assert outcome.date_max == "2026-11-16"


def test_quality_rejects_missing_required_column():
    data = "下单时间,商家编码,买家实付金额\n2026-11-10,SKU1,99\n".encode("utf-8")
    table = read_table(data, "x.csv")
    mapping = {"dt": "下单时间", "sku_id": "商家编码", "pay_amount": "买家实付金额"}
    outcome = quality.check("orders", table, mapping)
    assert outcome.verdict == "reject"
    assert any("订单号" in reason for reason in outcome.reasons)


def test_quality_warns_on_broken_money_identity():
    data = (
        "订单编号,下单时间,商家编码,买家实付金额\n"
        "A1,2026-11-10,SKU1,50\n"
        "A2,2026-11-10,SKU1,60\n"
    ).encode("utf-8")
    table = read_table(data, "x.csv")
    mapping = {
        "order_id": "订单编号",
        "dt": "下单时间",
        "sku_id": "商家编码",
        "pay_amount": "买家实付金额",
    }
    # 没有单价时无法验算，不该误报
    assert quality.check("orders", table, mapping).verdict == "pass"

    with_price = (
        "订单编号,下单时间,商家编码,数量,商品单价,买家实付金额\n"
        "B1,2026-11-10,SKU1,1,100,100\n"
        "B2,2026-11-10,SKU1,1,100,900\n"
    ).encode("utf-8")
    table2 = read_table(with_price, "y.csv")
    mapping2 = {
        "order_id": "订单编号",
        "dt": "下单时间",
        "sku_id": "商家编码",
        "qty": "数量",
        "unit_price": "商品单价",
        "pay_amount": "买家实付金额",
    }
    outcome = quality.check("orders", table2, mapping2)
    assert outcome.identity_checked == 2
    assert outcome.identity_failed == 1
    assert outcome.verdict == "warn"


# ---------- 预览与入库 ----------


# 本机 %TEMP% 不可用（见 docs/tech-compat.md），因此不用 pytest 的 tmp_path。
WORK = Path(__file__).resolve().parents[1] / ".tmp" / "ingest_tests"


@pytest.fixture()
def sandbox(db_path: Path):
    """独立库 + 独立工作目录，避免把样例数据写进全量测试库。"""
    import uuid

    root = WORK / uuid.uuid4().hex[:8]
    root.mkdir(parents=True)
    target = root / "store.db"
    shutil.copy2(db_path, target)
    try:
        yield target, root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _count(db: Path, table: str) -> int:
    import sqlite3

    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_preview_then_commit_writes_rows(sandbox):
    db, root = sandbox
    before = _count(db, "fact_orders")
    preview = service.save_preview(
        (SAMPLES / "taobao_orders.csv").read_bytes(), "taobao_orders.csv", root=root
    )
    assert preview["report"]["verdict"] == "pass"
    assert preview["dataset"] == "orders"
    result = service.commit_preview(preview["token"], db_path=str(db), root=root)
    assert result["inserted"] == 15
    assert _count(db, "fact_orders") == before + 15


def test_commit_is_idempotent(sandbox):
    db, root = sandbox
    data = (SAMPLES / "taobao_orders.csv").read_bytes()
    first = service.commit_preview(
        service.save_preview(data, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
    )
    second = service.commit_preview(
        service.save_preview(data, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
    )
    assert first["inserted"] == 15
    # 第二次：一行都不新增、也不「变更」，全部是「无变化」——这才是幂等该有的样子
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 15


def test_commit_requires_confirm_when_amount_changes(sandbox):
    db, root = sandbox
    data = (SAMPLES / "taobao_orders.csv").read_bytes()
    service.commit_preview(
        service.save_preview(data, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
    )

    # 同一订单、数量与实付都变了（模拟商家改单后重新导出），金额恒等式仍然成立
    changed = data.decode("utf-8-sig").replace(
        "TB20261110002,2026-11-10,SKU9002,1,199.00,0.00,199.00",
        "TB20261110002,2026-11-10,SKU9002,2,199.00,0.00,398.00",
    ).encode("utf-8")
    assert changed != data
    preview = service.save_preview(changed, "taobao_orders.csv", root=root)
    with pytest.raises(store.ChangesNeedConfirm) as caught:
        service.commit_preview(preview["token"], db_path=str(db), root=root)
    diff = caught.value.diff
    assert diff["updated"] == 1
    assert diff["unchanged"] == 14
    fields = {c["field"] for c in diff["samples"][0]["changes"]}
    assert {"qty", "pay_amount"} <= fields

    confirmed = service.commit_preview(
        service.save_preview(changed, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
        confirm_changes=True,
    )
    assert confirmed["updated"] == 1
    assert confirmed["unchanged"] == 14


def test_refund_id_is_stable_regardless_of_row_order(sandbox):
    db, root = sandbox
    before = _count(db, "fact_refunds")
    header = "订单编号,退款时间,退款金额,售后原因\n"
    one = "SHOP-1,2026-11-10,59.00,尺码不合适\nSHOP-2,2026-11-10,88.00,不喜欢\n"
    two = "SHOP-2,2026-11-10,88.00,不喜欢\nSHOP-1,2026-11-10,59.00,尺码不合适\n"
    for body in (one, two):
        preview = service.save_preview((header + body).encode("utf-8"), "refunds.csv", root=root)
        assert preview["dataset"] == "refunds", preview
        service.commit_preview(
            preview["token"], db_path=str(db), root=root, confirm_changes=True
        )
    # 没有售后单号时，行序变了也不能变成四条
    assert _count(db, "fact_refunds") == before + 2


def test_undo_restores_previous_values_and_deletes_new_rows(sandbox):
    db, root = sandbox
    before = _count(db, "fact_orders")
    data = (SAMPLES / "taobao_orders.csv").read_bytes()
    first = service.commit_preview(
        service.save_preview(data, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
    )
    assert first["inserted"] == 15
    assert first["undoable"] is True

    undone = service.undo_run(first["run_id"], db_path=str(db))
    assert undone["deleted"] == 15
    assert undone["restored"] == 0
    assert _count(db, "fact_orders") == before

    with pytest.raises(ValueError):
        service.undo_run(first["run_id"], db_path=str(db))


def test_undo_restores_old_amount(sandbox):
    db, root = sandbox
    order_id = "TB20261110002"
    data = (SAMPLES / "taobao_orders.csv").read_bytes()
    service.commit_preview(
        service.save_preview(data, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
    )
    changed = data.decode("utf-8-sig").replace(
        "TB20261110002,2026-11-10,SKU9002,1,199.00,0.00,199.00",
        "TB20261110002,2026-11-10,SKU9002,2,199.00,0.00,398.00",
    ).encode("utf-8")
    second = service.commit_preview(
        service.save_preview(changed, "taobao_orders.csv", root=root)["token"],
        db_path=str(db),
        root=root,
        confirm_changes=True,
    )
    assert second["updated"] == 1

    import sqlite3

    def _pay() -> tuple[int, float]:
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT qty, pay_amount FROM fact_orders WHERE order_id = ?", (order_id,)
            ).fetchone()

    assert _pay() == (2, 398.0)
    undone = service.undo_run(second["run_id"], db_path=str(db))
    assert undone["restored"] == 1
    assert undone["deleted"] == 0
    assert _pay() == (1, 199.0)


def test_commit_rejects_bad_token(sandbox):
    db, root = sandbox
    with pytest.raises(service.PreviewError):
        service.commit_preview("nope", db_path=str(db), root=root)


def test_preview_rejects_bad_quality(sandbox):
    db, root = sandbox
    broken = "订单编号,下单时间,商品编码\nA1,2026-11-10,SKU1\n".encode("utf-8")
    # 缺「实付金额」：预览阶段就该拦住，不该生成一个永远入库不了的 token
    with pytest.raises(service.PreviewError):
        service.save_preview(broken, "broken.csv", root=root)


def test_all_four_datasets_import(sandbox):
    db, root = sandbox
    for name in ("taobao_orders.csv", "douyin_traffic.csv", "shopify_products.csv", "erp_refunds.csv"):
        preview = service.save_preview((SAMPLES / name).read_bytes(), name, root=root)
        service.commit_preview(
            preview["token"], db_path=str(db), root=root, confirm_changes=True
        )
    caps = quality.capabilities(str(db))
    assert set(caps["available"]) >= {"orders", "traffic", "products", "refunds"}
    assert "gmv" not in caps["missing_metrics"]


# ---------- HTTP 接口 ----------


@pytest.fixture()
def client(sandbox, monkeypatch):
    from app.ingest import api as ingest_api

    db, root = sandbox
    monkeypatch.setattr(ingest_api, "_db", lambda: str(db))
    monkeypatch.setattr(ingest_api, "_root", lambda: root)
    from app.main import create_app

    return TestClient(create_app()), db


def test_schema_endpoint_lists_datasets(client):
    http, _db = client
    payload = http.get("/api/ingest/schema").json()
    names = {d["name"] for d in payload["datasets"]}
    assert names == set(DATASETS)
    assert payload["accepts"] == [".csv", ".xlsx"]


def test_preview_and_commit_over_http(client):
    http, db = client
    with open(SAMPLES / "taobao_orders.csv", "rb") as handle:
        response = http.post(
            "/api/ingest/preview",
            files={"file": ("taobao_orders.csv", handle, "text/csv")},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dataset"] == "orders"
    assert payload["report"]["verdict"] == "pass"

    commit = http.post("/api/ingest/commit", json={"token": payload["token"]})
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted"] == 15

    runs = http.get("/api/ingest/runs").json()
    assert runs["count"] >= 1
    assert runs["items"][0]["dataset"] == "orders"

    caps = http.get("/api/ingest/capabilities").json()
    assert "orders" in caps["available"]


def test_preview_rejects_unsupported_file(client):
    http, _db = client
    response = http.post(
        "/api/ingest/preview",
        files={"file": ("data.xls", io.BytesIO(b"whatever"), "application/vnd.ms-excel")},
    )
    assert response.status_code == 400


def test_commit_without_token_returns_400(client):
    http, _db = client
    assert http.post("/api/ingest/commit", json={}).status_code == 400


def _upload(http, name: str, data: bytes):
    return http.post(
        "/api/ingest/preview",
        files={"file": (name, io.BytesIO(data), "text/csv")},
    )


def test_commit_conflict_returns_409_then_accepts_confirmation(client):
    http, _db = client
    data = (SAMPLES / "taobao_orders.csv").read_bytes()
    first = _upload(http, "taobao_orders.csv", data).json()
    assert http.post("/api/ingest/commit", json={"token": first["token"]}).status_code == 200

    changed = data.decode("utf-8-sig").replace(
        "TB20261110002,2026-11-10,SKU9002,1,199.00,0.00,199.00",
        "TB20261110002,2026-11-10,SKU9002,2,199.00,0.00,398.00",
    ).encode("utf-8")
    preview = _upload(http, "taobao_orders.csv", changed).json()

    conflict = http.post("/api/ingest/commit", json={"token": preview["token"]})
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["code"] == "changes_need_confirm"
    assert detail["diff"]["updated"] == 1

    accepted = http.post(
        "/api/ingest/commit",
        json={"token": preview["token"], "confirm_changes": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["updated"] == 1


def test_undo_over_http(client):
    http, db = client
    import sqlite3

    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]

    preview = _upload(http, "taobao_orders.csv", (SAMPLES / "taobao_orders.csv").read_bytes()).json()
    committed = http.post("/api/ingest/commit", json={"token": preview["token"]}).json()
    assert committed["inserted"] == 15

    undo = http.post("/api/ingest/undo", json={"run_id": committed["run_id"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["deleted"] == 15

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0] == before

    again = http.post("/api/ingest/undo", json={"run_id": committed["run_id"]})
    assert again.status_code == 409

    missing = http.post("/api/ingest/undo", json={"run_id": "run_nope"})
    assert missing.status_code == 404

    runs = http.get("/api/ingest/runs").json()["items"]
    assert runs[0]["status"] == "undone"
