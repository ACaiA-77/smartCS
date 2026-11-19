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
from policies.consent import has_copilot_local_write_consent, is_copilot_action_confirmation

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
        "",
        "怎么向日记里写入内容？",
        "如何编译 Wiki 页面？",
        "保存草稿的流程是什么？",
        "能不能帮我整理笔记吗？",
        "有哪些工具可以写入？",
    ],
)
def test_consultation_is_not_local_write_consent(message):
    assert has_copilot_local_write_consent(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "帮我把今天的会议纪要记到日记里",
        "请保存一封邮件草稿",
        "把这个方案整理并归档到 Wiki",
        "新增一个本体实体 Project Alpha",
        "针对 notes/alpha.md 生成修改提案",
    ],
)
def test_explicit_local_write_request_is_consent(message):
    assert has_copilot_local_write_consent(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "确认发送",
        "好的，确认执行",
        "确认提交这条评论",
        "确认创建 Issue",
    ],
)
def test_copilot_action_confirmation_accepts_explicit_phrases(message):
    assert is_copilot_action_confirmation(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "取消",
        "确认发送算了",
        "先不发了，取消",
        "帮我给 alice 发一封邮件",
        "好的收到",
    ],
)
def test_copilot_action_confirmation_rejects_cancel_and_ambiguous_phrases(message):
    assert is_copilot_action_confirmation(message) is False


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
