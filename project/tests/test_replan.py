"""``app.diagnose.replan`` 的纯函数单元测试。

目标不是「正常卡能出结论」，而是把模块 docstring 的 1–6 条判定优先级逐条钉死：
support 不出条目 → 数据口径 skip → 补证 upgrade / 缺证 drop → 无辅证走 z 硬线
→ scope 消失 drop / 否则 demote，外加两个辅助函数的边界。

约定：
- 卡片与 verdict 都是普通 dict（``DiagnosisCard.as_dict`` / ``Verdict.as_dict`` 的形状）；
- ``rechecks`` 直接按 ``ScopeRecheck.as_dict`` 的形状构造，不 import recheck；
- 不查库、不打模型：这些用例不依赖数据库，可离线秒跑。
"""

from __future__ import annotations

import json

import pytest

from app.diagnose.replan import (
    ACTION_DEMOTE,
    ACTION_DROP,
    ACTION_SKIP,
    ACTION_UPGRADE,
    DATA_QUALITY_CHECKS,
    NO_REQUIRED_CODES,
    REQUIRED_SUPPORT,
    SAMPLE_Z_FLOOR,
    failed_checks,
    max_evidence_z,
    plan_recovery,
)

SCOPE = ("sku_id", "SKU0001")
OTHER_SCOPE = ("sku_id", "SKU9999")


# ---------------------------------------------------------------- 造数据


def _ev(metric: str = "gmv", z: object = 3.0, delta_pct: float = -0.2) -> dict:
    """按 DiagnosisCard.evidence 的字段造一条证据（只留判定用得到的）。"""
    return {
        "metric": metric,
        "display": metric,
        "delta_pct": delta_pct,
        "z": z,
    }


def _card(
    card_id: str,
    code: str,
    *,
    scope: tuple[str, str] = SCOPE,
    evidence: list[dict] | None = None,
) -> dict:
    return {
        "card_id": card_id,
        "code": code,
        "title": f"{code} 测试卡",
        "primary_metric": "gmv",
        "scope": {"dim": scope[0], "dim_value": scope[1]},
        "evidence": [_ev()] if evidence is None else evidence,
    }


def _chk(name: str, ok: bool = False) -> dict:
    return {"name": name, "ok": ok, "weight": 0.8, "detail": f"{name} 测试检查项"}


def _verdict(
    card_id: str,
    code: str,
    *,
    verdict: str = "weaken",
    checks: list[dict] | None = None,
) -> dict:
    return {
        "card_id": card_id,
        "code": code,
        "verdict": verdict,
        "checks": list(checks or []),
    }


def _recheck(
    scope: tuple[str, str] = SCOPE,
    *,
    scope_found: bool = True,
    required_metric: str | None = None,
    required_found: bool = False,
) -> dict:
    """按 ``ScopeRecheck.as_dict()`` 的形状造一份复检结果。"""
    return {
        "scope": list(scope),
        "days": 14,
        "as_of": "2026-11-18",
        "scope_found": scope_found,
        "required_metric": required_metric,
        "required_found": required_found,
        "metrics": [],
        "points": [],
        "elapsed_ms": 0.7,
    }


def _rechecks(recheck: dict, scope: tuple[str, str] = SCOPE) -> dict:
    return {scope: recheck}


# ---------------------------------------------------------------- 常量


def test_常量与契约一致():
    """这些常量是 replan 与 critic 的共享词汇，被改名会让判定静默失效。"""
    assert (ACTION_UPGRADE, ACTION_DROP, ACTION_DEMOTE, ACTION_SKIP) == (
        "upgrade",
        "drop",
        "demote",
        "skip",
    )
    assert NO_REQUIRED_CODES == {"R1", "R2", "R7", "R8"}
    # R6 是「双证据」根因（neg_review_rate 或 dsr 任一成立即可），不进必需辅证表：
    # 只认 neg_review_rate 会把「评分掉了、差评率还没起来」的真 R6 误删（实测 08-20）。
    assert "R6" not in REQUIRED_SUPPORT
    assert REQUIRED_SUPPORT == {
        "R3": "stockout_rate",
        "R4": "return_rate",
        "R5": "roi",
        "R9": "unit_price",
        "R10": "refund_rate",
    }
    assert SAMPLE_Z_FLOOR == 2.5
    assert DATA_QUALITY_CHECKS == {"promo", "market"}


# ---------------------------------------------------------------- 1. support


def test_support_不产生条目():
    """已成立的卡第二轮无事可做：哪怕有必需辅证、反面结论也不该冒出来。"""
    cards = [
        _card("c1", "R1"),
        _card("c2", "R3"),
    ]
    verdicts = [
        _verdict("c1", "R1", verdict="support", checks=[_chk("sample")]),
        _verdict("c2", "R3", verdict="support"),
    ]
    out = plan_recovery(cards, verdicts, _rechecks(_recheck(required_found=True)))
    assert out == [], f"support 的卡不应产生任何 Recovery，实际产生了 {[r.as_dict() for r in out]}"


# ---------------------------------------------------------------- 2. 数据口径


@pytest.mark.parametrize(
    "checks",
    [
        pytest.param([_chk("promo")], id="promo"),
        pytest.param([_chk("market")], id="market"),
        pytest.param([_chk("promo"), _chk("market")], id="promo+market"),
    ],
)
def test_失败检查只落在数据口径则skip(checks):
    """促销/大盘口径问题硬凑只会造假；优先级高于补证（即便 required_found=True）。"""
    out = plan_recovery(
        [_card("c1", "R4")],
        [_verdict("c1", "R4", checks=checks)],
        _rechecks(_recheck(required_found=True)),
    )
    assert [r.action for r in out] == [ACTION_SKIP], (
        f"checks={[c['name'] for c in checks]} 应 skip，实际 {[r.as_dict() for r in out]}"
    )


def test_混合检查不全是数据口径则不skip():
    """{promo, sample} 说明还有别的毛病，不能按「口径问题」一推了之，要走后续规则。"""
    out = plan_recovery(
        [_card("c1", "R7")],
        [_verdict("c1", "R7", checks=[_chk("promo"), _chk("sample")])],
        _rechecks(_recheck(scope_found=True)),
    )
    assert [r.action for r in out] == [ACTION_DEMOTE], (
        f"promo+sample 不应 skip，而应按规则 5/6 得到 demote，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- 3. upgrade


@pytest.mark.parametrize(("code", "metric"), sorted(REQUIRED_SUPPORT.items()))
def test_必需辅证补到则upgrade(code, metric):
    """宽窗补到必需辅证 → upgrade；证据 z 很弱也不该在这里被丢（z 硬线排在 3 之后）。"""
    card = _card("c1", code, evidence=[_ev("gmv", 0.5)])
    verdict = _verdict("c1", code, checks=[_chk("sample")])
    out = plan_recovery(
        [card],
        [verdict],
        _rechecks(_recheck(scope_found=False, required_metric=metric, required_found=True)),
    )
    assert len(out) == 1, f"{code}: 应产生 1 条 Recovery"
    item = out[0]
    assert item.action == ACTION_UPGRADE, (
        f"{code}: required_found=True 应 upgrade，实际 {item.action}（reason={item.reason}）"
    )
    assert item.required_metric == metric, f"{code}: required_metric 应为 {metric}，实际 {item.required_metric}"
    assert item.scope == SCOPE
    assert item.checks == ["sample"]


def test_upgrade_优先于scope消失():
    """规则 3 在规则 6 之前：补到辅证就不该因为 scope_found=False 被丢。"""
    out = plan_recovery(
        [_card("c1", "R3")],
        [_verdict("c1", "R3", checks=[_chk("upstream")])],
        _rechecks(_recheck(scope_found=False, required_metric="stockout_rate", required_found=True)),
    )
    assert [r.action for r in out] == [ACTION_UPGRADE], (
        f"scope_found=False 但 required_found=True 应 upgrade，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- 4. drop（缺辅证）


def test_必需辅证查不到则drop():
    """有必需辅证但宽窗复检没补到 → drop，即使 scope 还在、证据 z 很强。"""
    out = plan_recovery(
        [_card("c1", "R3")],
        [_verdict("c1", "R3", checks=[_chk("upstream")])],
        _rechecks(_recheck(scope_found=True, required_metric="stockout_rate", required_found=False)),
    )
    assert [r.action for r in out] == [ACTION_DROP], (
        f"required_found=False 应 drop，实际 {[r.as_dict() for r in out]}"
    )


def test_无复检信息且有必需辅证则drop():
    """rechecks=None 视为「没有复检信息」，有必需辅证的卡按规则 4 drop。"""
    out = plan_recovery(
        [_card("c1", "R4")],
        [_verdict("c1", "R4", checks=[_chk("upstream")])],
        None,
    )
    assert [r.action for r in out] == [ACTION_DROP], (
        f"rechecks=None 且有必需辅证应 drop，实际 {[r.as_dict() for r in out]}"
    )


def test_复检里没有该scope也按缺信息drop():
    """rechecks 里只有别的 scope：等同于查不到该卡的复检结果。"""
    out = plan_recovery(
        [_card("c1", "R4", scope=OTHER_SCOPE)],
        [_verdict("c1", "R4", checks=[_chk("upstream")])],
        _rechecks(_recheck(scope=SCOPE, required_found=True)),
    )
    assert [r.action for r in out] == [ACTION_DROP], (
        f"rechecks 键与卡片 scope 不一致时应 drop，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- 5/6. 无必需辅证


@pytest.mark.parametrize("code", ["R1", "R2", "R7", "R8"])
def test_NO_REQUIRED_CODES的scope消失仍demote(code):
    """规则 5：R1/R2/R7/R8 永不因 scope 消失被丢。

    宽窗改变了促销日占比，同 scope 在 14 天窗里看不到不代表异常不存在
    （实测 11-17 的 R7、11-11 的 R1 都是真因）。
    """
    out = plan_recovery(
        [_card("c1", code)],
        [_verdict("c1", code, checks=[_chk("upstream")])],
        _rechecks(_recheck(scope_found=False)),
    )
    assert [r.action for r in out] == [ACTION_DEMOTE], (
        f"{code}: NO_REQUIRED_CODES 即便 scope_found=False 也应 demote，"
        f"实际 {[r.as_dict() for r in out]}"
    )


@pytest.mark.parametrize("code", ["R1", "R2", "R7", "R8", "R99"])
def test_无必需辅证_scope仍在则demote(code):
    """无必需辅证的卡 scope 还在：只降权、不丢弃（规则 5/6 的 else 分支）。"""
    out = plan_recovery(
        [_card("c1", code)],
        [_verdict("c1", code, checks=[_chk("upstream")])],
        _rechecks(_recheck(scope_found=True)),
    )
    assert [r.action for r in out] == [ACTION_DEMOTE], (
        f"{code}: scope_found=True 应 demote，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- z 硬线


@pytest.mark.parametrize("scope_found", [True, False], ids=["scope还在", "scope消失"])
def test_z硬线_证据全弱则drop(scope_found):
    """所有证据 |z| 都低于 2.5 视为噪声 → drop。

    z 硬线排在规则 5 之前，所以对 NO_REQUIRED_CODES（R7）同样生效，
    而且 scope 在不在都救不回来。
    """
    out = plan_recovery(
        [_card("c1", "R7", evidence=[_ev("gmv", 1.2), _ev("ctr", -2.4)])],
        [_verdict("c1", "R7", checks=[_chk("sample")])],
        _rechecks(_recheck(scope_found=scope_found)),
    )
    assert [r.action for r in out] == [ACTION_DROP], (
        f"|z| 全为 1.2/2.4（< {SAMPLE_Z_FLOOR}）应 drop（scope_found={scope_found}），"
        f"实际 {[r.as_dict() for r in out]}"
    )


def test_z硬线_可关闭():
    """sample_z_floor=None 关闭硬线：R7 退回规则 5，即便 scope 消失也只 demote。"""
    cards = [_card("c1", "R7", evidence=[_ev("gmv", 1.2), _ev("ctr", -2.4)])]
    verdicts = [_verdict("c1", "R7", checks=[_chk("sample")])]
    rechecks = _rechecks(_recheck(scope_found=False))
    out = plan_recovery(cards, verdicts, rechecks, sample_z_floor=None)
    assert [r.action for r in out] == [ACTION_DEMOTE], (
        f"关闭 z 硬线后应 demote，实际 {[r.as_dict() for r in out]}"
    )


def test_z硬线_恰好等于阈值不丢():
    """硬线是严格小于：|z| 正好 2.5 不应判成噪声。"""
    out = plan_recovery(
        [_card("c1", "R1", evidence=[_ev("gmv", 2.5)])],
        [_verdict("c1", "R1", checks=[_chk("sample")])],
        _rechecks(_recheck(scope_found=True)),
    )
    assert [r.action for r in out] == [ACTION_DEMOTE], (
        f"|z|=2.5 不满足「< {SAMPLE_Z_FLOOR}」，应 demote，实际 {[r.as_dict() for r in out]}"
    )


def test_z硬线_不覆盖upgrade():
    """补证成功比 z 大：z 硬线排在 3/4 之后，弱证据的卡仍应 upgrade。"""
    out = plan_recovery(
        [_card("c1", "R4", evidence=[_ev("return_rate", 0.3)])],
        [_verdict("c1", "R4", checks=[_chk("sample")])],
        _rechecks(_recheck(required_metric="return_rate", required_found=True)),
    )
    assert [r.action for r in out] == [ACTION_UPGRADE], (
        f"弱 z + required_found=True 应 upgrade，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- 未知 code


def test_未知根因code按无辅证兜底():
    """R99 不在 REQUIRED_SUPPORT 也不在 NO_REQUIRED_CODES：走规则 6。"""
    rechecks = _rechecks(_recheck(scope_found=True))
    verdict = _verdict("c1", "R99", checks=[_chk("upstream")])
    strong = plan_recovery([_card("c1", "R99")], [verdict], rechecks)
    assert [r.action for r in strong] == [ACTION_DEMOTE], (
        f"R99 + 强证据 + scope 仍在应 demote，实际 {[r.as_dict() for r in strong]}"
    )
    assert strong[0].required_metric is None, "R99 没有必需辅证，required_metric 应为 None"

    weak = plan_recovery(
        [_card("c1", "R99", evidence=[_ev("gmv", 1.0)])],
        [verdict],
        rechecks,
    )
    assert [r.action for r in weak] == [ACTION_DROP], (
        f"R99 + 弱证据应走 z 硬线 drop，实际 {[r.as_dict() for r in weak]}"
    )


def test_未知根因code的scope消失则drop():
    """规则 6：只有未知根因才因「宽窗里 scope 没了」被丢。"""
    out = plan_recovery(
        [_card("c1", "R99")],
        [_verdict("c1", "R99", checks=[_chk("upstream")])],
        _rechecks(_recheck(scope_found=False)),
    )
    assert [r.action for r in out] == [ACTION_DROP], (
        f"R99 + scope_found=False 应 drop，实际 {[r.as_dict() for r in out]}"
    )


# ---------------------------------------------------------------- 输入/输出形态


def test_verdict里未知card_id被忽略():
    """verdicts 里出现 cards 中不存在的 card_id 时直接跳过，不能补造条目。"""
    cards = [_card("c1", "R1")]
    verdicts = [
        _verdict("ghost", "R1", checks=[_chk("sample")]),
        _verdict("c1", "R1", checks=[_chk("sample")]),
    ]
    out = plan_recovery(cards, verdicts, _rechecks(_recheck(scope_found=True)))
    assert [r.card_id for r in out] == ["c1"], (
        f"未知 card_id 应被忽略，实际产出 {[r.card_id for r in out]}"
    )


def test_多项按card_id升序且可json序列化():
    """输出顺序按 card_id 字符串升序（c1 < c10 < c2），字段齐全且可 JSON 序列化。"""
    cards = [_card("c10", "R1"), _card("c2", "R1"), _card("c1", "R1")]
    verdicts = [
        _verdict("c10", "R1", checks=[_chk("sample")]),
        _verdict("c1", "R1", checks=[_chk("sample")]),
        _verdict("c2", "R1", checks=[_chk("sample")]),
    ]
    out = plan_recovery(cards, verdicts, _rechecks(_recheck(scope_found=True)))
    assert [r.card_id for r in out] == ["c1", "c10", "c2"], (
        f"应按 card_id 升序，实际 {[r.card_id for r in out]}"
    )

    payload = [r.as_dict() for r in out]
    for item in payload:
        assert set(item) == {
            "card_id",
            "code",
            "action",
            "reason",
            "scope",
            "required_metric",
            "checks",
        }, f"as_dict 字段与契约不符：{sorted(item)}"
        assert isinstance(item["scope"], list), "as_dict 的 scope 应转成 list（可 JSON 序列化）"
        assert item["scope"] == ["sku_id", "SKU0001"]
        assert item["checks"] == ["sample"]
        assert isinstance(item["reason"], str) and item["reason"]
    text = json.dumps(payload, ensure_ascii=False)
    assert "c10" in text


def test_scope缺字段时as_dict仍可序列化():
    """卡片 scope 不完整（dim 缺失）时不应炸：scope 归一为 None。"""
    card = _card("c1", "R1")
    card["scope"] = {"dim": "", "dim_value": ""}
    out = plan_recovery(
        [card],
        [_verdict("c1", "R1", checks=[_chk("sample")])],
        _rechecks(_recheck(scope_found=True)),
    )
    assert len(out) == 1
    assert out[0].scope is None
    assert out[0].as_dict()["scope"] is None


# ---------------------------------------------------------------- 辅助函数


def test_max_evidence_z():
    """取证据里最大的 |z|；无证据/缺 z 返回 0.0，字符串 z 不应炸。"""
    assert max_evidence_z({}) == 0.0
    assert max_evidence_z({"evidence": []}) == 0.0
    assert max_evidence_z({"evidence": [{"metric": "gmv"}]}) == 0.0
    assert max_evidence_z({"evidence": [{"z": None}]}) == 0.0
    assert max_evidence_z({"evidence": [{"z": -4.0}, {"z": 1.5}]}) == pytest.approx(4.0)
    assert max_evidence_z({"evidence": [{"z": "3.2"}]}) == pytest.approx(3.2)
    assert max_evidence_z({"evidence": [{"z": 2.0}, {"z": "oops"}]}) == pytest.approx(2.0)


def test_failed_checks_去重保序且忽略通过项():
    verdict = {
        "checks": [
            {"name": "sample", "ok": False},
            {"name": "upstream", "ok": True},
            {"name": "promo", "ok": False},
            {"name": "sample", "ok": False},
            {"name": "impact"},  # 缺 ok：按通过处理
            {"name": "market", "ok": False},
        ]
    }
    assert failed_checks(verdict) == ["sample", "promo", "market"], (
        f"应去重保序只留 ok is False，实际 {failed_checks(verdict)}"
    )


def test_failed_checks_空输入():
    assert failed_checks({}) == []
    assert failed_checks({"checks": []}) == []
    assert failed_checks({"checks": None}) == []
