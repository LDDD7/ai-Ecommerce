"""加载并校验策略卡目录。

约定：`app/playbook/cards/*.yaml`，一个文件一张卡，文件名与 card_id 对应。
加载失败时**收集所有错误再抛**，一次暴露全部问题，避免改一张跑一次。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.playbook.schema import Card

CARDS_DIR = Path(__file__).resolve().parent / "cards"


class PlaybookError(ValueError):
    """策略卡加载/校验失败，message 里带文件与字段位置。"""


def _load_one(path: Path) -> Card:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlaybookError(f"{path.name}: YAML 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise PlaybookError(f"{path.name}: 顶层必须是字典")
    try:
        card = Card(**raw)
    except Exception as exc:  # pydantic.ValidationError 也是 Exception
        raise PlaybookError(f"{path.name}: {exc}") from exc
    if path.stem != card.card_id:
        raise PlaybookError(f"{path.name}: 文件名应与 card_id 一致（{card.card_id}）")
    return card


def load_cards(cards_dir: str | Path | None = None) -> list[Card]:
    """加载目录下全部 YAML 卡，按 card_id 排序。目录不存在时返回空列表。"""
    root = Path(cards_dir) if cards_dir is not None else CARDS_DIR
    if not root.exists():
        return []

    # 只认 .yaml / .yml，`*.y*ml` 过宽（会匹配 .yxml 之类）
    paths = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])

    loaded: list[tuple[Path, Card]] = []
    errors: list[str] = []
    for path in paths:
        try:
            loaded.append((path, _load_one(path)))
        except PlaybookError as exc:
            errors.append(str(exc))

    seen: dict[str, Path] = {}
    for path, card in loaded:
        if card.card_id in seen:
            errors.append(f"{card.card_id}: 重复定义（{seen[card.card_id].name} 与 {path.name}）")
        else:
            seen[card.card_id] = path
    cards = [card for _, card in loaded]

    if errors:
        raise PlaybookError("策略卡校验失败：\n- " + "\n- ".join(errors))
    return sorted(cards, key=lambda c: c.card_id)


def by_root(cards: list[Card]) -> dict[str, list[Card]]:
    """按根因码索引，诊断卡拿到根因后直接取候选卡。"""
    index: dict[str, list[Card]] = {}
    for card in cards:
        for code in card.root_causes:
            index.setdefault(code, []).append(card)
    return index


def get_card(card_id: str, cards: list[Card] | None = None) -> Card | None:
    for card in cards if cards is not None else load_cards():
        if card.card_id == card_id:
            return card
    return None
