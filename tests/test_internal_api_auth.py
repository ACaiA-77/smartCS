"""Phase 1/2 acceptance for the /internal/auth/verify contract (design §6).

Moved into python-impl/tests/ in Phase 2 (D8 裁决). Runs against the real
FastAPI router and a real MySQL database.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.service_jwt import decode_service_token
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
    service_token,
    user_token,
)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    """Per-test credentials AND database, so nothing leaks into the suite."""
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    # Without this the app would read the live database while the seeds go to
    # the isolated test one.
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)


@pytest_asyncio.fixture
async def client():
    apply_migration()
    app = await build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_happy_path_returns_authoritative_identity(client):
    account_id = seed_account(random_username("happy"), "bu-happy")
    seed_session("sess-happy", account_id, "pi")

    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-happy"},
        headers=service_header(account_id=account_id, session_id="sess-happy"),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "account_id": account_id,
        "business_user_id": "bu-happy",
        "status": "active",
        "session_id": "sess-happy",
        "harness_version": "pi",
    }


@pytest.mark.asyncio
async def test_missing_or_invalid_service_token_is_401(client):
    account_id = seed_account(random_username("svc"), "bu-svc")
    seed_session("sess-svc", account_id, "pi")
    payload = {"user_jwt": user_token(account_id), "session_id": "sess-svc"}

    assert (await client.post("/internal/auth/verify", json=payload)).status_code == 401
    assert (
        await client.post("/internal/auth/verify", json=payload, headers={"Authorization": "Bearer nonsense"})
    ).status_code == 401
    assert (
        await client.post(
            "/internal/auth/verify",
            json=payload,
            headers=service_header(account_id=account_id, session_id="sess-svc", secret=USER_SECRET),
        )
    ).status_code == 401
    assert (
        await client.post(
            "/internal/auth/verify",
            json=payload,
            headers=service_header(account_id=account_id, session_id="sess-svc", audience="smartcs-pi-harness"),
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_service_token_ttl_is_capped(client):
    account_id = seed_account(random_username("ttl"), "bu-ttl")
    seed_session("sess-ttl", account_id, "pi")
    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-ttl"},
        headers=service_header(account_id=account_id, session_id="sess-ttl", ttl=600),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_user_token_is_403(client):
    account_id = seed_account(random_username("bad"), "bu-bad")
    seed_session("sess-bad", account_id, "pi")

    for token in [
        "not-a-token",
        user_token(account_id, secret="a-different-user-secret-0123456789abcd"),
        user_token(account_id, ttl=-10),
    ]:
        response = await client.post(
            "/internal/auth/verify",
            json={"user_jwt": token, "session_id": "sess-bad"},
            headers=service_header(account_id=account_id, session_id="sess-bad"),
        )
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_disabled_account_is_403(client):
    account_id = seed_account(random_username("disabled"), "bu-disabled", status="disabled")
    seed_session("sess-disabled", account_id, "pi")
    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-disabled"},
        headers=service_header(account_id=account_id, session_id="sess-disabled"),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_session_of_another_account_is_404(client):
    owner = seed_account(random_username("owner"), "bu-owner")
    intruder = seed_account(random_username("intruder"), "bu-intruder")
    seed_session("sess-owned", owner, "pi")

    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(intruder), "session_id": "sess-owned"},
        headers=service_header(account_id=intruder, session_id="sess-owned"),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_legacy_session_is_409(client):
    account_id = seed_account(random_username("legacy"), "bu-legacy")
    seed_session("sess-legacy", account_id, "legacy")
    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-legacy"},
        headers=service_header(account_id=account_id, session_id="sess-legacy"),
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_session_id_must_match_the_service_token(client):
    account_id = seed_account(random_username("mismatch"), "bu-mismatch")
    seed_session("sess-a", account_id, "pi")
    seed_session("sess-b", account_id, "pi")
    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-b"},
        headers=service_header(account_id=account_id, session_id="sess-a"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_body_cannot_smuggle_identity(client):
    account_id = seed_account(random_username("smuggle"), "bu-smuggle")
    seed_session("sess-smuggle", account_id, "pi")
    response = await client.post(
        "/internal/auth/verify",
        json={"user_jwt": user_token(account_id), "session_id": "sess-smuggle", "business_user_id": "bu-other"},
        headers=service_header(account_id=account_id, session_id="sess-smuggle"),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_optional_business_user_id_claim_is_cross_checked(client):
    account_id = seed_account(random_username("claim"), "bu-claim")
    seed_session("sess-claim", account_id, "pi")
    payload = {"user_jwt": user_token(account_id), "session_id": "sess-claim"}

    ok = await client.post(
        "/internal/auth/verify",
        json=payload,
        headers=service_header(account_id=account_id, session_id="sess-claim", business_user_id="bu-claim"),
    )
    assert ok.status_code == 200

    bad = await client.post(
        "/internal/auth/verify",
        json=payload,
        headers=service_header(account_id=account_id, session_id="sess-claim", business_user_id="bu-wrong"),
    )
    assert bad.status_code == 401


def test_verify_token_may_omit_business_user_id_but_tools_may_not():
    """Phase 2 / D5: the requirement differs per endpoint."""
    verify_token = service_token(account_id=5, session_id="s", business_user_id=None)
    identity = decode_service_token(verify_token, require_business_user_id=False)
    assert identity.business_user_id is None

    import jwt as pyjwt

    with pytest.raises(pyjwt.InvalidTokenError):
        decode_service_token(verify_token, require_business_user_id=True)

    tool_token = service_token(account_id=5, session_id="s", business_user_id="bu-5")
    assert decode_service_token(tool_token, require_business_user_id=True).business_user_id == "bu-5"
