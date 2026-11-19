"""Deterministic consent rules for WRITE side effects.

A model's intent is never consent. ``internal_api/write_authorization.py`` reads
these functions against the raw user message stored in ``memory_source_event``
before any LLM work begins.
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
