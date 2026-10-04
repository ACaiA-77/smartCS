"""Phase 3 acceptance for POST /internal/context/turn-snapshot (design §2)."""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    build_order_repository,
    build_user_memory_service,
    random_username,
    seed_account,
    seed_session,
    service_header,
)


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.delenv("SMARTCS_TURN_SNAPSHOT_BUDGET", raising=False)


@pytest_asyncio.fixture
async def snapshot(tmp_path):
    apply_migration()
    repository = build_order_repository(tmp_path)
    memory = await build_user_memory_service()
    app = await build_app(order_repository=repository, user_memory_service=memory)
    account_id = seed_account(random_username("snap"), "user_001")
    session_id = "sess-snapshot"
    seed_session(session_id, account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "repo": repository,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_001",
        }


def _call(snapshot, **overrides):
    headers = service_header(
        account_id=overrides.pop("account_id", snapshot["account_id"]),
        session_id=overrides.pop("session_id", snapshot["session_id"]),
        business_user_id=overrides.pop("business_user_id", snapshot["business_user_id"]),
        **overrides,
    )
    return snapshot["http"].post(
        "/internal/context/turn-snapshot",
        json={"session_id": snapshot["session_id"], "client_request_id": "req-1"},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_snapshot_returns_authoritative_blocks(snapshot):
    response = await _call(snapshot)
    assert response.status_code == 200, response.text
    body = response.json()

    kinds = [block["kind"] for block in body["blocks"]]
    assert "protected_fields" in kinds
    assert "user_profile" in kinds
    assert body["tokenBudget"] > 0

    protected = next(b for b in body["blocks"] if b["kind"] == "protected_fields")
    # Real business data for the authenticated user, straight from the order
    # repository — compare against what that repository actually holds rather
    # than a hard-coded id (the demo dataset has 100 orders).
    expected = {order["order_id"] for order in snapshot["repo"].list_orders_for_user("user_001", limit=3)}
    assert expected, "the demo dataset should hold orders for user_001"
    assert set(re.findall(r"ORD-\d{8}-\d{4}", protected["content"])) == expected
    # Protected identifiers come from the shared context/ extractor, not a
    # bespoke list in this module.
    assert protected["protected"]["orders"]
    # The shared extractor nests by index, so assert on the flattened shape.
    assert '"order_id"' in json.dumps(protected["protected"])

    profile = next(b for b in body["blocks"] if b["kind"] == "user_profile")
    assert "business_user_id=user_001" in profile["content"]


@pytest.mark.asyncio
async def test_snapshot_is_deterministic_and_llm_free(snapshot):
    first = (await _call(snapshot)).json()
    second = (await _call(snapshot, client_request_id="req-2")).json()
    assert first["blocks"] == second["blocks"]


@pytest.mark.asyncio
async def test_snapshot_never_leaks_another_users_orders(snapshot):
    """P3-2: the snapshot is scoped to the authenticated identity."""
    body = (await _call(snapshot)).json()
    text = " ".join(block["content"] for block in body["blocks"])
    shown = set(re.findall(r"ORD-\d{8}-\d{4}", text))
    assert shown, "snapshot should carry the user's own order facts"

    # Every order shown belongs to the authenticated user...
    for order_id in shown:
        assert snapshot["repo"].get_order_for_user(order_id, "user_001") is not None, order_id
        # ...and to nobody else.
        assert snapshot["repo"].get_order_for_user(order_id, "user_002") is None, order_id


@pytest.mark.asyncio
async def test_snapshot_requires_business_user_id_and_ownership(snapshot):
    # Missing business_user_id on the service token.
    assert (await _call(snapshot, business_user_id=None)).status_code == 401

    # Another account's session.
    stranger = seed_account(random_username("snap-stranger"), "user_003")
    response = await _call(snapshot, account_id=stranger, business_user_id="user_003")
    assert response.status_code == 404

    # Token naming a different session than the body.
    mismatched = service_header(
        account_id=snapshot["account_id"], session_id="other-session", business_user_id=snapshot["business_user_id"]
    )
    response = await snapshot["http"].post(
        "/internal/context/turn-snapshot",
        json={"session_id": snapshot["session_id"], "client_request_id": "req-1"},
        headers=mismatched,
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_snapshot_respects_the_char_budget(snapshot, monkeypatch):
    monkeypatch.setenv("SMARTCS_TURN_SNAPSHOT_BUDGET", "60")
    body = (await _call(snapshot)).json()
    total = sum(len(block["content"]) for block in body["blocks"])
    assert body["tokenBudget"] == 200  # floor enforced
    assert total <= 200 + 64  # bounded (small overshoot only from the last block)
