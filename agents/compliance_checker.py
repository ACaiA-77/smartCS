"""Apple support policy, privacy, and safety checker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call


@dataclass
class ComplianceResult:
    passed: bool
    risk_level: str
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sanitized_content: str = ""


SENSITIVE_PATTERNS = {
    "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "bank_card": r"(?<!\d)\d{16,19}(?!\d)",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}
FORBIDDEN_TERMS = [
    "保证退款",
    "立即全额退款",
    "永久免费维修",
    "绕过激活锁",
    "破解 Apple ID",
    "获取他人账户",
]

COMPLIANCE_SYSTEM_PROMPT = """你是 Apple 售后客服的安全与隐私审查 Agent。

检查客服回复是否：
1. 泄露或要求密码、验证码、身份证件、银行卡或其他个人信息；
2. 承诺未经授权的退款、维修结果、账户冻结或处理时效；
3. 协助绕过设备、账户或激活锁安全机制；
4. 包含歧视性、侮辱性或不安全指导。

只返回 JSON：
{"passed": true/false, "risk_level": "low|medium|high|critical", "violations": [], "suggestions": []}
"""


class ComplianceCheckerAgent:
    """Runs deterministic PII/policy checks before an optional LLM review."""

    def __init__(self, llm: ChatOpenAI, enable_llm_review: bool = True):
        self.llm = llm
        self.enable_llm_review = enable_llm_review
        self.decision_audit: list[dict[str, Any]] = []

    def _rule_based_check(self, content: str) -> list[str]:
        violations = []
        for term in FORBIDDEN_TERMS:
            if term in content:
                violations.append(f"检测到不安全承诺或请求: '{term}'")
        for pii_type, pattern in SENSITIVE_PATTERNS.items():
            if re.search(pattern, content):
                label = {"phone": "手机号", "id_card": "身份证号", "bank_card": "银行卡号", "email": "邮箱地址"}[pii_type]
                violations.append(f"检测到 PII 信息: {label}")
        return violations

    def _mask_pii(self, content: str) -> str:
        def mask_match(match: re.Match[str]) -> str:
            text = match.group()
            prefix = min(2, max(1, len(text) // 3))
            suffix = min(2, max(1, (len(text) - prefix) // 3))
            mask_length = max(1, len(text) - prefix - suffix)
            return text[:prefix] + "*" * mask_length + text[-suffix:]

        masked = content
        for pattern in SENSITIVE_PATTERNS.values():
            masked = re.sub(pattern, mask_match, masked)
        return masked

    def _record_audit(self, result: ComplianceResult) -> None:
        self.decision_audit.append(
            {
                "passed": result.passed,
                "risk_level": result.risk_level,
                "violation_count": len(result.violations),
                "violation_types": [violation.split(":", 1)[0] for violation in result.violations],
            }
        )
        del self.decision_audit[:-100]

    @trace_agent_call("compliance_rule_check")
    async def rule_check(self, content: str) -> ComplianceResult:
        violations = self._rule_based_check(content)
        sanitized = self._mask_pii(content)
        if not violations:
            return ComplianceResult(passed=True, risk_level="low", sanitized_content=sanitized)
        has_pii = any("PII" in violation for violation in violations)
        has_unsafe = any("不安全" in violation for violation in violations)
        risk_level = "critical" if has_pii and has_unsafe else "high" if has_pii or has_unsafe else "medium"
        return ComplianceResult(False, risk_level, violations=violations, sanitized_content=sanitized)

    @trace_agent_call("compliance_llm_check")
    async def llm_check(self, content: str) -> ComplianceResult:
        response = await self.llm.ainvoke(
            [SystemMessage(content=COMPLIANCE_SYSTEM_PROMPT), HumanMessage(content=f"请审查以下客服回复内容：\n\n{content}")]
        )
        import json

        try:
            result = json.loads(response.content)
        except (TypeError, json.JSONDecodeError):
            return ComplianceResult(
                passed=True,
                risk_level="low",
                suggestions=["llm_review_unavailable"],
                sanitized_content=self._mask_pii(content),
            )
        return ComplianceResult(
            passed=bool(result.get("passed", True)),
            risk_level=str(result.get("risk_level", "low")),
            violations=list(result.get("violations", [])),
            suggestions=list(result.get("suggestions", [])),
            sanitized_content=self._mask_pii(content),
        )

    @trace_agent_call("compliance_full_check")
    async def full_check(self, content: str) -> ComplianceResult:
        rule_result = await self.rule_check(content)
        if not rule_result.passed and rule_result.risk_level in {"high", "critical"}:
            self._record_audit(rule_result)
            return rule_result
        if not self.enable_llm_review:
            self._record_audit(rule_result)
            return rule_result
        llm_result = await self.llm_check(content)
        risk_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        result = ComplianceResult(
            passed=rule_result.passed and llm_result.passed,
            risk_level=max((rule_result.risk_level, llm_result.risk_level), key=lambda value: risk_priority.get(value, 0)),
            violations=rule_result.violations + llm_result.violations,
            suggestions=llm_result.suggestions,
            sanitized_content=rule_result.sanitized_content,
        )
        self._record_audit(result)
        return result

    @trace_agent_call("compliance_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        sub_results = state.get("sub_results", {})
        content_to_check = "\n".join(result for result in sub_results.values() if isinstance(result, str))
        if not content_to_check.strip() and state.get("final_response"):
            content_to_check = state["final_response"]
        if not content_to_check.strip():
            return {**state, "compliance_passed": True}
        result = await self.full_check(content_to_check)
        if not result.passed:
            for key, value in list(sub_results.items()):
                if isinstance(value, str):
                    sub_results[key] = self._mask_pii(value)
        return {
            **state,
            "compliance_passed": result.passed,
            "sub_results": {
                **sub_results,
                "compliance": {
                    "passed": result.passed,
                    "risk_level": result.risk_level,
                    "violations": result.violations,
                    "audit_event": self.decision_audit[-1] if self.decision_audit else {},
                },
            },
        }
