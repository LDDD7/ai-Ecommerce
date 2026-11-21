"""把 Critic 的驳回理由翻译成「第二轮动作」（R1 契约）。

定位：纯函数层。不查库、不联网、不读配置，只做判定。
输入是 Critic 产出的卡片字典、Verdict 字典，外加可选的「同 scope 复检结果」
（由 :mod:`app.diagnose.recheck` 提供，形状见 ``ScopeRecheck.as_dict``）。

四类动作
--------
``upgrade``：同一 scope 在更宽窗口里补到了该根因的**必需辅证** → 重新计分，有机会转成立。
``drop``   ：证据链站不住（scope 消失，或必需辅证查不到）→ 从结果里撤掉。
``demote`` ：根因本身没有「必需辅证」这一说（R1/R2/R7/R8）→ 保留但降权，永不因此丢弃。
``skip``   ：不归第二轮管（已成立、口径问题如促销/大盘、影响金额太小）。

判定优先级（实现必须严格按此顺序，先命中先返回）
------------------------------------------------
1. ``verdict == "support"`` → **不产生条目**（已经成立，第二轮无事可做）。
2. 失败检查只落在 ``{"promo", "market"}`` → ``skip``（口径问题，硬凑只会造假）。
3. 有必需辅证（见 ``REQUIRED_SUPPORT``）且复检结果 ``required_found=True`` → ``upgrade``。
4. 有必需辅证，但复检 ``required_found=False``（无论 scope 是否还在）→ ``drop``。
5. 无必需辅证但属于 ``NO_REQUIRED_CODES`` → 只过 6 的 z 硬线，最终 ``demote``。
   **永不因「scope 消失」丢弃**：宽窗会改变促销日占比，scope 消失不等于异常不存在
   （实测 11-17 的 R7、11-11 的 R1 都是真因，宽窗却看不到同 scope）。
6. 其它未知根因：z 硬线不过 → ``drop``；``scope_found is False`` → ``drop``；否则 ``demote``。

关于 ``sample_z_floor``：Critic 的 `sample` 检查只看权重，这里再给一条硬线——
若卡片**所有证据**的 ``|z|`` 都低于该阈值（默认 2.5），视为噪声 → ``drop``。
该判定排在 3/4 之后（补证成功就不该因为 z 小被丢掉），排在 5/6 之前。
传 ``None`` 可关闭这条硬线（消融用）。

关于「没有 scope 的卡」：规则 4 依赖复检结果，而 ``rechecks`` 以 scope 为键。
若某张有必需辅证的卡没有 scope（当前 ``build_diagnosis`` 不会产出这种卡），
``_find_recheck`` 查不到 → 按规则 4 的「缺信息」分支 ``drop``。这是有意为之：
宁可撤掉一张无法复核的卡，也不让它带着未经验证的置信度进报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# ---- 动作常量 -------------------------------------------------------------

ACTION_UPGRADE = "upgrade"
ACTION_DROP = "drop"
ACTION_DEMOTE = "demote"
ACTION_SKIP = "skip"

ACTIONS = (ACTION_UPGRADE, ACTION_DROP, ACTION_DEMOTE, ACTION_SKIP)

# ---- 判定所需的领域常量 ---------------------------------------------------

#: 这些根因没有「必需辅证」：主症状本身即可支撑，缺辅证不构成丢卡理由。
NO_REQUIRED_CODES: frozenset[str] = frozenset({"R1", "R2", "R7", "R8"})

#: 根因 → 必须出现的辅证指标（与 ``app.diagnose.critic.REQUIRED_SUPPORT`` 保持一致）。
#: R6（差评爆发）不收：它的两个证据（``neg_review_rate`` 与 ``dsr``）任一个都算成立，
#: 只认 ``neg_review_rate`` 会把「评分掉但差评率还没起来」的真 R6 误删（实测 08-20）。
REQUIRED_SUPPORT: dict[str, str] = {
    "R3": "stockout_rate",
    "R4": "return_rate",
    "R5": "roi",
    "R9": "unit_price",
    "R10": "refund_rate",
}

#: 证据 |z| 硬线：全部低于它就是噪声。
SAMPLE_Z_FLOOR = 2.5

#: 这些检查失败属于「数据口径」问题，不归第二轮管。
DATA_QUALITY_CHECKS: frozenset[str] = frozenset({"promo", "market"})


@dataclass(slots=True)
class Recovery:
    """一张卡的第二轮处置结论。"""

    card_id: str
    code: str
    action: str
    reason: str
    scope: tuple[str, str] | None = None
    required_metric: str | None = None
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "code": self.code,
            "action": self.action,
            "reason": self.reason,
            "scope": list(self.scope) if self.scope else None,
            "required_metric": self.required_metric,
            "checks": list(self.checks),
        }


def max_evidence_z(card: Mapping[str, Any]) -> float:
    """卡片证据里最大的 |z|；没有证据或 z 缺失时返回 0.0。"""
    best = 0.0
    for ev in card.get("evidence") or []:
        try:
            z = abs(float(ev.get("z") or 0.0))
        except (TypeError, ValueError):
            continue
        best = max(best, z)
    return best


def failed_checks(verdict: Mapping[str, Any]) -> list[str]:
    """取出一条 verdict 里「没过」的检查名（按出现顺序）。"""
    names: list[str] = []
    for chk in verdict.get("checks") or []:
        if isinstance(chk, Mapping) and not chk.get("ok", True):
            name = str(chk.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def plan_recovery(
    cards: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Mapping[str, Any]],
    rechecks: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    *,
    sample_z_floor: float | None = SAMPLE_Z_FLOOR,
) -> list[Recovery]:
    """为每张被 Critic 削弱/驳回的卡给出第二轮动作。

    参数
    ----
    cards
        ``build_diagnosis`` 里的卡片字典，需要 ``card_id`` / ``code`` /
        ``scope{dim,dim_value}`` / ``evidence[]``。
    verdicts
        Critic 的 verdict 字典，需要 ``card_id`` / ``code`` / ``verdict`` /
        ``checks[]{name,ok}``。
    rechecks
        键为 ``(dim, dim_value)``，值为 ``ScopeRecheck.as_dict()`` 的映射。
        缺省为 ``None``，此时视为「没有复检信息」——有必需辅证的卡按 4 处理（drop）。
    sample_z_floor
        ``None`` 关闭「证据全弱即丢弃」这条硬线。

    返回
    ----
    ``list[Recovery]``，按 ``card_id`` 升序排列；``support`` 的卡不出现（视为 skip 不发消息）。
    ``verdicts`` 里出现但 ``cards`` 里找不到的 ``card_id`` 直接忽略。
    """
    cards_by_id = _cards_by_id(cards)

    out: list[Recovery] = []
    for verdict in verdicts:
        card_id = _text(verdict.get("card_id"))
        if not card_id:
            # 没有 card_id 的 verdict 无从对应，直接忽略。
            continue
        card = cards_by_id.get(card_id)
        if card is None:
            # 卡不在输入里（例如已被上游摘掉），不产生条目。
            continue

        code = _text(card.get("code")) or _text(verdict.get("code"))
        scope = _card_scope(card)
        checks = failed_checks(verdict)
        decision = _decide(verdict, card, code, scope, checks, rechecks, sample_z_floor)
        if decision is None:
            # support：已成立，第二轮无事可做。
            continue
        action, reason = decision
        out.append(
            Recovery(
                card_id=card_id,
                code=code,
                action=action,
                reason=reason,
                scope=scope,
                required_metric=REQUIRED_SUPPORT.get(code),
                checks=checks,
            )
        )

    out.sort(key=lambda item: item.card_id)
    return out


# ---- 私有辅助 -------------------------------------------------------------


def _text(value: Any) -> str:
    """把任意字段收敛成去空白的字符串；None 变成空串。"""
    return "" if value is None else str(value).strip()


def _cards_by_id(cards: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """``card_id`` → 卡片；重复 id 保留第一张，缺失 id 的卡忽略。"""
    out: dict[str, Mapping[str, Any]] = {}
    for card in cards:
        card_id = _text(card.get("card_id"))
        if card_id and card_id not in out:
            out[card_id] = card
    return out


def _card_scope(card: Mapping[str, Any]) -> tuple[str, str] | None:
    """取卡片的 ``(dim, dim_value)``；形状不对或字段缺失时返回 ``None``。"""
    raw = card.get("scope")
    if not isinstance(raw, Mapping):
        return None
    dim = _text(raw.get("dim"))
    value = _text(raw.get("dim_value"))
    if not dim or not value:
        return None
    return (dim, value)


def _find_recheck(
    rechecks: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    scope: tuple[str, str] | None,
) -> Mapping[str, Any] | None:
    """按 scope 取复检结果；无复检、无 scope 或结果不是映射时返回 ``None``。"""
    if not rechecks or scope is None:
        return None
    found = rechecks.get(scope)
    return found if isinstance(found, Mapping) else None


def _decide(
    verdict: Mapping[str, Any],
    card: Mapping[str, Any],
    code: str,
    scope: tuple[str, str] | None,
    checks: list[str],
    rechecks: Mapping[tuple[str, str], Mapping[str, Any]] | None,
    sample_z_floor: float | None,
) -> tuple[str, str] | None:
    """按模块 docstring 的 1–6 优先级给出 ``(action, reason)``；support 返回 ``None``。"""
    # 1. 已成立：不产生条目。
    if _text(verdict.get("verdict")) == "support":
        return None

    # 2. 失败检查只落在数据口径上：第二轮不补救。
    if checks and all(name in DATA_QUALITY_CHECKS for name in checks):
        return ACTION_SKIP, "数据口径问题，不由第二轮补救"

    # 3/4. 有必需辅证：看复检有没有把它补回来。
    metric = REQUIRED_SUPPORT.get(code)
    if metric:
        recheck = _find_recheck(rechecks, scope)
        if recheck is None:
            return ACTION_DROP, "缺少复检信息，无法补证"
        if bool(recheck.get("required_found")):
            return ACTION_UPGRADE, f"同 scope 宽窗补到 {metric} 辅证"
        return ACTION_DROP, f"宽窗内查不到 {metric} 辅证"

    # 5. 无必需辅证的根因：先过证据强度硬线。
    if sample_z_floor is not None:
        strongest = max_evidence_z(card)
        if strongest < sample_z_floor:
            return ACTION_DROP, (
                f"证据全部偏弱（|z| 最高 {strongest:.1f} < {sample_z_floor:.1f}）"
            )

    # 6. NO_REQUIRED_CODES：宽窗里 scope 消失不算证据，保留降权。
    if code in NO_REQUIRED_CODES:
        return ACTION_DEMOTE, "保留但降权（该根因无必需辅证，宽窗不看 scope）"

    # 6'. 未知根因：scope 在宽窗里消失就丢，否则保留降权。
    recheck = _find_recheck(rechecks, scope)
    if recheck is not None and recheck.get("scope_found") is False:
        return ACTION_DROP, "宽窗内该 scope 已无异常"
    return ACTION_DEMOTE, "保留但降权"
