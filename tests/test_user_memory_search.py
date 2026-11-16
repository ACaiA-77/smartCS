"""Lexical matching rules for user-memory retrieval, pinned on their own."""

from __future__ import annotations

import pytest

from memory.user_memory_search import score, search_terms, semantic_text, tokens


def test_cjk_text_matches_by_bigram():
    # Single CJK characters are dropped by the length rule; two-character
    # overlaps carry the match (this is the pre-existing ranking behaviour).
    body = tokens("退款流程")
    assert body == ["退款", "款流", "流程"]


def test_identifiers_survive_tokenization():
    assert "ord_20260801_0001" in tokens("订单 ORD_20260801_0001 到哪了")


def test_stop_words_and_single_characters_do_not_match_except_a_and_i():
    body = tokens("the a i 的 了 ok")
    assert "the" not in body and "的" not in body
    assert "a" in body and "i" in body


def test_semantic_text_skips_provenance_fields():
    text = semantic_text({
        "value": "用户偏好短信通知",
        "source": "session-1",
        "session_id": "s-1",
        "confidence": 0.9,
        "derived_abstraction": {"text": "不应检索"},
    })
    assert "短信通知" in text
    assert "session-1" not in text and "0.9" not in text and "不应检索" not in text


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["  Refund  ", "REFUND", "订单123", ""], ["refund", "订单123"]),
        (["has space", "ok_term"], ["ok_term"]),
        (["x" * 65], []),
    ],
)
def test_search_terms_are_normalized_unique_and_bounded(raw, expected):
    assert search_terms(raw) == expected


def test_search_terms_are_capped_at_twenty_four():
    assert len(search_terms([f"term{index}" for index in range(40)])) == 24


def test_score_is_the_share_of_query_terms_present():
    assert score({"退款", "流程"}, "退款流程说明") > 0
    assert score({"退款"}, "物流进度") == 0.0
    assert score(set(), "任何文本") == 0.0
