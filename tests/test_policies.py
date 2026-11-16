"""The neutral rule layer: what it decides, and what it is allowed to import."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from policies.compliance_rules import (
    FORBIDDEN_TERMS,
    PII_LABELS,
    SENSITIVE_PATTERNS,
    mask_pii,
    rule_review,
    rule_violations,
)
from policies.consent import has_explicit_create_consent, is_refund_confirmation

ROOT = Path(__file__).resolve().parents[1]


def test_forbidden_term_is_reported():
    violations = rule_violations("我们保证收益，稳赚不赔")
    assert "包含违规金融用语: '保证收益'" in violations
    assert "包含违规金融用语: '稳赚不赔'" in violations


def test_every_pii_class_is_reported_and_masked():
    content = "手机13800138000 身份证110101199003071234 卡号6222021234567890123 邮箱a.b@example.com"
    violations = rule_violations(content)
    for pii_type in SENSITIVE_PATTERNS:
        assert f"检测到PII信息泄露: {PII_LABELS[pii_type]}" in violations
    masked = mask_pii(content)
    assert "13800138000" not in masked
    assert "a.b@example.com" not in masked


def test_rule_review_pairs_violations_with_the_masked_text():
    violations, masked = rule_review("保证收益 13800138000")
    assert violations
    assert masked != "保证收益 13800138000"


def test_forbidden_table_keeps_the_regulated_phrases():
    assert "保证收益" in FORBIDDEN_TERMS
    assert set(SENSITIVE_PATTERNS) == {"phone", "id_card", "bank_card", "email"}


@pytest.mark.parametrize(
    "message",
    [
        "我要投诉流程是什么？",
        "我要投诉的话怎么操作？",
        "怎么帮我创建投诉工单？",
        "我要申请理赔需要什么条件？",
        "开户需要什么材料？",
    ],
)
def test_consultation_is_not_create_consent(message):
    assert has_explicit_create_consent(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "我要投诉服务问题",
        "请帮我创建投诉工单",
        "帮我提交投诉",
        "我要申请理赔",
        "帮我办理开户",
    ],
)
def test_explicit_business_request_is_create_consent(message):
    assert has_explicit_create_consent(message) is True


@pytest.mark.parametrize("message", ["确认退款", "好的 确认提交退款 ORD-20260801-0001"])
def test_refund_confirmation_accepts_the_two_confirmed_shapes(message):
    assert is_refund_confirmation(message) is True


@pytest.mark.parametrize("message", ["取消退款", "不退了", "先不退", "算了", "帮我退款"])
def test_refund_confirmation_rejects_cancel_and_request_phrases(message):
    assert is_refund_confirmation(message) is False


def test_the_rule_layer_imports_nothing_above_it():
    """`policies` is the bottom of the graph: no agent, API or langchain import."""
    script = (
        "import sys, policies.compliance_rules, policies.consent;"
        "forbidden = [m for m in sys.modules if m.split('.')[0] in "
        "{'agents', 'api', 'internal_api', 'context', 'memory', 'rag', 'tool_runtime', 'langchain_core'}];"
        "print(','.join(sorted(forbidden)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
