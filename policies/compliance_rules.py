"""Forbidden financial phrases, PII patterns and the deterministic checks over them.

Single source of truth: ``agents/compliance_checker.py`` (the legacy rule stage)
and ``internal_api/compliance.py`` (the internal review gate) both call these
definitions instead of keeping their own copies. Before this module they held
one table and two copies of the masking / violation logic.

No I/O, no model, no configuration: given a string, the answer is deterministic.
"""

from __future__ import annotations

import re

SENSITIVE_PATTERNS = {
    "phone": r"1[3-9]\d{9}",
    "id_card": r"\d{17}[\dXx]",
    "bank_card": r"\d{16,19}",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}

FORBIDDEN_TERMS = [
    "保证收益", "稳赚不赔", "零风险", "保本保息",
    "最高收益", "预期收益率", "承诺回报",
    "内部消息", "内幕", "暗箱操作",
]

PII_LABELS = {
    "phone": "手机号",
    "id_card": "身份证号",
    "bank_card": "银行卡号",
    "email": "邮箱地址",
}


def _mask_match(match: re.Match) -> str:
    text = match.group()
    if len(text) <= 4:
        return "****"
    return text[:3] + "*" * (len(text) - 6) + text[-3:]


def mask_pii(content: str) -> str:
    """Replace every PII match with a partially masked form."""
    masked = content
    for pattern in SENSITIVE_PATTERNS.values():
        masked = re.sub(pattern, _mask_match, masked)
    return masked


def rule_violations(content: str) -> list[str]:
    """Every deterministic violation in ``content``, in a stable order."""
    violations: list[str] = []
    for term in FORBIDDEN_TERMS:
        if term in content:
            violations.append(f"包含违规金融用语: '{term}'")
    for pii_type, pattern in SENSITIVE_PATTERNS.items():
        if re.search(pattern, content):
            violations.append(f"检测到PII信息泄露: {PII_LABELS.get(pii_type, pii_type)}")
    return violations


def rule_review(content: str) -> tuple[list[str], str]:
    """Return ``(violations, masked_text)``."""
    return rule_violations(content), mask_pii(content)
