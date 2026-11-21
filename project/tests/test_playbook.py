"""策略库测试：27 张卡的完整性、根因覆盖、坏数据拒绝与检索字段。"""

from __future__ import annotations

import copy
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from app.playbook import (
    ACTION_TYPES,
    ROOT_CAUSES,
    SCOPES,
    Card,
    PlaybookError,
    by_root,
    get_card,
    load_cards,
)

EXPECTED_CARDS = 27


@pytest.fixture
def tmp_path():
    """本机 %TEMP% 共享目录会被其他沙箱进程占住，pytest 内置 tmp_path 建目录时报
    WinError 5（见 tests/conftest.py 注释）；这里覆盖成工作区 .tmp/ 下的独立目录。
    """
    path = Path(__file__).resolve().parents[1] / ".tmp" / "playbook" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# 一张合法卡作为「好数据」模板，坏数据用例在它上面改一处。
_VALID_CARD: dict = {
    "card_id": "PB-R4-01",
    "title": "补齐尺码表与试穿建议",
    "root_causes": ["R4"],
    "metrics": ["refund_rate", "return_rate"],
    "risk": 1,
    "scope": ["sku", "style"],
    "action_type": "swap_creative",
    "preconditions": ["退款原因中「尺码偏小」占比超过 30%"],
    "steps": ["详情页首屏加尺码表", "主图第二张换成尺码对照卡"],
    "expected_effect": {
        "metric": "refund_rate",
        "direction": "down",
        "range": [0.15, 0.35],
        "window_days": 14,
    },
    "success_metric": "return_rate",
    "source": "测试",
    "version": 1,
}


def _card_data(**overrides) -> dict:
    data = copy.deepcopy(_VALID_CARD)
    data.update(overrides)
    return data


def _write_yaml(tmp_path: Path, stem: str, data: dict) -> Path:
    path = tmp_path / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _bad_cases() -> list:
    bad_effect = copy.deepcopy(_VALID_CARD["expected_effect"])
    bad_effect["range"] = [0.35, 0.15]
    return [
        pytest.param(_card_data(root_causes=["R99"]), id="unknown-root"),
        pytest.param(_card_data(unexpected_field="多出来的字段"), id="extra-field"),
        pytest.param(_card_data(expected_effect=bad_effect), id="reversed-range"),
        pytest.param(_card_data(steps=["只写了一步"]), id="one-step"),
    ]


# ---- 真实卡片目录 ----


def test_card_ids_unique_and_sorted():
    ids = [card.card_id for card in load_cards()]
    assert len(ids) == len(set(ids)), f"card_id 有重复：{ids}"
    assert ids == sorted(ids), f"卡未按 card_id 排序：{ids}"


def test_every_root_cause_has_at_least_one_card():
    index = by_root(load_cards())
    missing = [code for code in ROOT_CAUSES if not index.get(code)]
    assert not missing, f"以下根因没有任何卡片覆盖：{missing}"


def test_card_count_matches_expected():
    cards = load_cards()
    assert len(cards) == EXPECTED_CARDS, (
        f"策略卡应为 {EXPECTED_CARDS} 张，当前 {len(cards)} 张"
        "（其他 agent 可能还在写，属预期失败）"
    )


def test_search_text_contains_root_name_and_action():
    cards = load_cards()
    assert cards, "卡片目录为空，至少应有一张卡"
    for card in cards:
        text = card.search_text()
        assert card.card_id in text
        assert card.title in text
        for code in card.root_causes:
            assert ROOT_CAUSES[code] in text, f"{card.card_id} 的检索文本缺少根因名 {ROOT_CAUSES[code]}"
        assert card.action_type in text, f"{card.card_id} 的检索文本缺少动作类型"
        assert card.expected_effect.metric in text


def test_get_card_hit_and_miss():
    cards = load_cards()
    assert cards, "卡片目录为空，至少应有一张卡"
    target = cards[0]
    assert get_card(target.card_id, cards) is target
    assert get_card("PB-R99-99", cards) is None


# ---- 坏数据必须被拒 ----


@pytest.mark.parametrize("data", _bad_cases())
def test_reject_invalid_card(tmp_path, data):
    # 文件名与 card_id 一致，确保报错只来自字段校验而不是文件名规则
    _write_yaml(tmp_path, data["card_id"], data)
    with pytest.raises(PlaybookError):
        load_cards(tmp_path)


def test_reject_filename_card_id_mismatch(tmp_path):
    _write_yaml(tmp_path, "PB-R4-02", _card_data())
    with pytest.raises(PlaybookError) as excinfo:
        load_cards(tmp_path)
    assert "文件名" in str(excinfo.value)


# ---- 契约一致性（防 Literal 与常量漂移）----


def test_literal_matches_constants():
    from typing import get_args

    fields = Card.model_fields
    assert set(get_args(fields["action_type"].annotation)) == set(ACTION_TYPES)
    scope_item = get_args(fields["scope"].annotation)[0]
    assert set(get_args(scope_item)) == set(SCOPES)


def test_duplicate_card_id_reports_both_files(tmp_path):
    data = _card_data()
    _write_yaml(tmp_path, "PB-R4-01", data)
    (tmp_path / "PB-R4-01.yml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(PlaybookError) as excinfo:
        load_cards(tmp_path)
    message = str(excinfo.value)
    assert "重复定义" in message
    assert "PB-R4-01.yaml" in message and "PB-R4-01.yml" in message


def test_card_id_prefix_must_be_in_root_causes(tmp_path):
    _write_yaml(tmp_path, "PB-R5-01", _card_data(card_id="PB-R5-01"))
    with pytest.raises(PlaybookError) as excinfo:
        load_cards(tmp_path)
    assert "前缀" in str(excinfo.value)
