"""Deterministic consent rules for WRITE side effects.

A model's intent is never consent. Both callers read these functions against the
raw user message: ``agents/ticket_handler.py`` (legacy path) and
``internal_api/write_authorization.py`` (the authoritative write gate, which
reads the message back from ``memory_source_event`` rather than the request
body).

``has_explicit_create_consent`` used to exist as two verbatim copies — one in
each caller, with a "mirror any change" obligation in the docstring. This is
now the only copy.

Deliberately NOT unified here: ``agents/intent_router.py`` maps an exact set of
confirmation utterances while classifying legacy intent. That is a different
rule (exact match, different phrase list) and folding it in would change
legacy behaviour.
"""

from __future__ import annotations

import re


def _normalize(user_message: str) -> str:
    return re.sub(r"\s+", "", str(user_message))


def has_explicit_create_consent(user_message: str) -> bool:
    """LLM intent alone cannot authorize a ticket-creating side effect."""
    normalized = _normalize(user_message)
    if any(
        marker in normalized
        for marker in (
            "怎么", "如何", "怎样", "流程", "步骤", "规则", "政策",
            "条件", "入口", "在哪里", "需要什么", "需要哪些", "怎么办",
            "是什么", "有哪些要求",
        )
    ):
        return False
    if "工单" in normalized and any(
        phrase in normalized for phrase in ("帮我创建", "我要创建", "请创建", "提交工单", "发起工单")
    ):
        return True
    return any(
        phrase in normalized
        for phrase in (
            "我要投诉", "帮我投诉", "我要申请理赔", "帮我申请理赔", "我要开户",
            "帮我办理开户", "提交投诉", "提交申请", "创建工单", "发起申请",
        )
    )


def is_refund_confirmation(user_message: str) -> bool:
    """Deterministic confirmation rule for the refund's second phase.

    The confirmation turn is the one that says 确认退款, and the cancel phrases
    are explicitly NOT confirmations. Kept deliberately narrow — a model's
    paraphrase is not consent.
    """
    normalized = _normalize(user_message)
    if any(phrase in normalized for phrase in ("取消退款", "不退了", "先不退", "算了")):
        return False
    return "确认退款" in normalized or "确认提交退款" in normalized
