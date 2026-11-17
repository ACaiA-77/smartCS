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


_COPILOT_ACTION_CANCEL_MARKERS = ("取消", "不发了", "先不", "算了", "暂不", "别发")
_COPILOT_ACTION_CONFIRM_PHRASES = (
    "确认发送",
    "确认执行",
    "确认提交",
    "确认创建",
    "确认评论",
    "确认写入",
    "确认修改",
)

#: A question about how something works is never consent to write.
_LOCAL_WRITE_REJECT_MARKERS = (
    "怎么",
    "如何",
    "怎样",
    "流程",
    "步骤",
    "规则",
    "是什么",
    "有哪些",
    "吗",
    "？",
    "?",
    "是否",
    "能不能",
    "可不可以",
    "要不要",
    "有没有",
)

#: Explicit local-write action verbs.  The request itself must name one of
#: these; a bare greeting or a generic "帮我" is not a write request.
_LOCAL_WRITE_ACTION_MARKERS = (
    "保存",
    "存为",
    "存成",
    "存到",
    "存进",
    "记录",
    "记到",
    "记下",
    "追加",
    "写入",
    "编译",
    "整理",
    "归档",
    "更新",
    "修改",
    "起草",
    "提案",
    "生成",
    "添加",
    "新增",
    "录入",
    "同步",
    "创建",
    "补全",
    "合并",
)


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


def is_copilot_action_confirmation(user_message: str) -> bool:
    """Deterministic confirmation rule for a Copilot external action.

    The model cannot confirm its own proposal: the confirmation turn is the one
    whose raw user message says 确认发送/确认执行/... and does NOT say 取消/算了.
    Kept deliberately narrow — a paraphrase is not consent.
    """
    normalized = _normalize(user_message)
    if any(marker in normalized for marker in _COPILOT_ACTION_CANCEL_MARKERS):
        return False
    return any(phrase in normalized for phrase in _COPILOT_ACTION_CONFIRM_PHRASES)


def has_copilot_local_write_consent(user_message: str) -> bool:
    """Same-turn consent for a low-risk local write (vault/ontology/draft).

    This authorizes only local mutations.  External side effects always need the
    pending_action two-phase flow and :func:`is_copilot_action_confirmation`.
    Consultation questions are rejected first; otherwise the message must name
    an explicit local-write action.
    """
    normalized = _normalize(user_message)
    if not normalized:
        return False
    if any(marker in normalized for marker in _LOCAL_WRITE_REJECT_MARKERS):
        return False
    return any(marker in normalized for marker in _LOCAL_WRITE_ACTION_MARKERS)
