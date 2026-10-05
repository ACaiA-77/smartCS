"""Phase 3 acceptance for POST /internal/compliance/review (design §4).

The rule stage is deterministic and always runs; the LLM stage is off by
default and is never reached in these tests unless explicitly enabled with a
stub checker (no real endpoint is ever contacted).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.compliance import SAFE_FALLBACK_TEXT, rule_review
from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    random_username,
    seed_account,
    seed_session,
    service_header,
)


class _StubChecker:
    """Records whether it was consulted; returns a scripted verdict."""

    def __init__(self, passed: bool = True, violations=None):
        self.calls: list[str] = []
        self._passed = passed
        self._violations = violations or []

    async def full_check(self, content, *, state=None):
        self.calls.append(content)

        class _Result:
            pass

        result = _Result()
        result.passed = self._passed
        result.violations = self._violations
        result.sanitized_content = content
        return result


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.delenv("SMARTCS_COMPLIANCE_LLM_REVIEW", raising=False)


@pytest_asyncio.fixture
async def gate():
    apply_migration()
    app = await build_app()
    account_id = seed_account(random_username("comp"), "user_001")
    seed_session("sess-compliance", account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {"http": http, "app": app, "account_id": account_id, "session_id": "sess-compliance"}


def _review(gate, text, **overrides):
    headers = service_header(
        account_id=overrides.pop("account_id", gate["account_id"]),
        session_id=gate["session_id"],
        business_user_id=overrides.pop("business_user_id", "user_001"),
        **overrides,
    )
    return gate["http"].post(
        "/internal/compliance/review",
        json={
            "text": text,
            "session_id": gate["session_id"],
            "client_request_id": "req-comp",
        },
        headers=headers,
    )


@pytest.mark.asyncio
async def test_clean_text_passes(gate):
    response = await _review(gate, "您的订单已发货，预计明天送达。")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"verdict": "pass", "replacement": None, "rulesHit": [], "llmReviewed": False}


@pytest.mark.asyncio
async def test_pii_is_sanitized_never_passed_through(gate):
    """P3-4: the masked text is what the caller receives."""
    response = await _review(gate, "您的手机号 13800138000 已登记，我们会尽快联系您。")
    body = response.json()
    assert body["verdict"] == "sanitize"
    assert "13800138000" not in body["replacement"]
    assert "138*****000" in body["replacement"]
    assert any("PII" in hit for hit in body["rulesHit"])
    assert body["llmReviewed"] is False


@pytest.mark.asyncio
async def test_forbidden_financial_term_fails_to_the_safe_fallback(gate):
    """Masking cannot neutralise a forbidden phrase, so the whole text is replaced."""
    response = await _review(gate, "本产品保证收益，零风险，稳赚不赔。")
    body = response.json()
    assert body["verdict"] == "fail"
    assert body["replacement"] == SAFE_FALLBACK_TEXT
    assert body["rulesHit"]


@pytest.mark.asyncio
async def test_llm_stage_is_off_by_default_even_when_a_checker_exists(gate):
    checker = _StubChecker(passed=False, violations=["llm would reject"])
    gate["app"].state.compliance_checker = checker

    response = await _review(gate, "这是一段合规的普通回复。")
    body = response.json()
    assert body["verdict"] == "pass"
    assert body["llmReviewed"] is False
    assert checker.calls == [], "the LLM stage must not run unless enabled"


@pytest.mark.asyncio
async def test_llm_stage_runs_only_when_enabled(gate, monkeypatch):
    checker = _StubChecker(passed=False, violations=["llm rejected"])
    gate["app"].state.compliance_checker = checker
    monkeypatch.setenv("SMARTCS_COMPLIANCE_LLM_REVIEW", "true")

    response = await _review(gate, "这是一段合规的普通回复。")
    body = response.json()
    assert body["llmReviewed"] is True
    assert checker.calls == ["这是一段合规的普通回复。"]
    assert body["verdict"] == "fail"
    assert body["replacement"] == SAFE_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_llm_stage_failure_degrades_to_pass(gate, monkeypatch):
    class _Exploding:
        async def full_check(self, content, *, state=None):
            raise RuntimeError("reviewer down")

    gate["app"].state.compliance_checker = _Exploding()
    monkeypatch.setenv("SMARTCS_COMPLIANCE_LLM_REVIEW", "true")

    response = await _review(gate, "这是一段合规的普通回复。")
    body = response.json()
    # Rule-clean text is not blocked by a flaky optional stage.
    assert body["verdict"] == "pass"


@pytest.mark.asyncio
async def test_review_requires_identity_and_ownership(gate):
    assert (await _review(gate, "你好", business_user_id=None)).status_code == 401

    stranger = seed_account(random_username("comp-stranger"), "user_009")
    assert (await _review(gate, "你好", account_id=stranger)).status_code == 403


def test_shared_rule_table_is_the_source_of_truth():
    """The rule tables are imported from the existing checker, not duplicated."""
    from agents.compliance_checker import FORBIDDEN_TERMS, SENSITIVE_PATTERNS

    violations, masked = rule_review("保证收益 13800138000")
    assert any("违规金融用语" in v for v in violations)
    assert any("PII" in v for v in violations)
    assert set(SENSITIVE_PATTERNS) == {"phone", "id_card", "bank_card", "email"}
    assert "保证收益" in FORBIDDEN_TERMS
    assert masked != "保证收益 13800138000"
