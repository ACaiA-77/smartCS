"""Auth contracts plus opt-in MySQL persistence, with runtime-only random credentials."""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import AsyncMock, Mock

import httpx
import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from auth.context import UserContext, current_user
from auth.dependency import check_request_origin, cors_allowed_origins, get_current_user
from auth.jwt import COOKIE_NAME, ISSUER, TOKEN_TTL_SECONDS, cookie_options, decode_token, get_settings, issue_token
from auth.password import hash_password, verify_password
from platform_db import PlatformConflict, PlatformDatabase, Sessions, Users
from scripts.init_demo_auth_user import verify_business_user


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)


def test_password_is_salted_argon2_and_invalid_inputs_fail_closed():
    password = secrets.token_urlsafe(24)
    first, second = hash_password(password), hash_password(password)
    assert first.startswith("$argon2id$") and first != second
    assert verify_password(password, first)
    assert not verify_password(secrets.token_urlsafe(24), first)
    assert not verify_password(password, None)
    assert not verify_password(password, "not-a-hash")
    assert not verify_password("", first)
    assert not verify_password("x" * 1025, first)
    with pytest.raises(ValueError):
        hash_password("short")


def test_token_roundtrip_strict_configuration_and_cookie_contract(auth_env, monkeypatch):
    claims = decode_token(issue_token(17))
    assert claims["sub"] == "17" and claims["iss"] == ISSUER
    assert claims["exp"] - claims["iat"] == TOKEN_TTL_SECONDS
    assert set(claims) == {"sub", "iat", "exp", "iss", "jti"}
    assert claims["jti"] != decode_token(issue_token(17))["jti"]
    assert cookie_options() == {"httponly": True, "samesite": "strict", "secure": False, "path": "/"}
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    assert cookie_options()["secure"] is True
    for account_id in [False, 0, -1, "17", 2**63]:
        with pytest.raises(ValueError):
            issue_token(account_id)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "invalid")
    with pytest.raises(ValueError):
        get_settings()
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_JWT_SECRET")
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        issue_token(1)
    monkeypatch.setenv("AUTH_JWT_SECRET", secrets.token_urlsafe(8))
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        get_settings()


@pytest.mark.parametrize("case", ["expired", "future", "missing_sub", "missing_iat", "missing_exp",
    "missing_iss", "missing_jti", "issuer", "algorithm", "signature", "none", "subject",
    "leading_zero", "large_subject", "empty_jti", "long_lifetime", "iat_string", "exp_bool"])
def test_jwt_rejects_invalid_claims_and_signature(auth_env, case):
    now = int(time.time())
    claims = {"sub": "12", "iat": now, "exp": now + 300, "iss": ISSUER, "jti": uuid.uuid4().hex}
    key, algorithm = os.environ["AUTH_JWT_SECRET"], "HS256"
    if case.startswith("missing_"):
        del claims[case.removeprefix("missing_")]
    elif case == "expired":
        claims.update(iat=now - 600, exp=now - 300)
    elif case == "future":
        claims.update(iat=now + 600, exp=now + 900)
    elif case == "issuer":
        claims["iss"] = "another-service"
    elif case == "algorithm":
        algorithm = "HS384"
    elif case == "signature":
        key = secrets.token_urlsafe(48)
    elif case == "none":
        key, algorithm = "", "none"
    elif case == "subject":
        claims["sub"] = "user_001"
    elif case == "leading_zero":
        claims["sub"] = "012"
    elif case == "large_subject":
        claims["sub"] = str(2**63)
    elif case == "empty_jti":
        claims["jti"] = ""
    elif case == "long_lifetime":
        claims["exp"] = now + 86400
    elif case == "iat_string":
        claims["iat"] = str(now)
    elif case == "exp_bool":
        claims["exp"] = True
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_token(pyjwt.encode(claims, key, algorithm=algorithm))


class MemoryUsers:
    """Explicit dependency unit-test double; MySQL is tested separately below."""
    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}

    async def by_id(self, account_id):
        await asyncio.sleep(0)
        return self.rows.get(account_id)

    async def by_username(self, username):
        return next((row for row in self.rows.values() if row["username"] == username), None)


def dependency_app():
    app = FastAPI()
    app.state.platform_users = MemoryUsers([
        {"id": 1, "username": "alice", "business_user_id": "user_001", "status": "active"},
        {"id": 2, "username": "bob", "business_user_id": "user_002", "status": "active"},
    ])

    @app.api_route("/identity", methods=["GET", "POST"])
    async def identity(user: UserContext = Depends(get_current_user)):
        assert current_user.get() == user
        await asyncio.sleep(0.001)
        assert current_user.get() == user
        return asdict(user)

    @app.get("/failure")
    async def failure(user: UserContext = Depends(get_current_user)):
        assert current_user.get() == user
        raise HTTPException(409, "test failure")

    @app.post("/login-origin")
    async def login_origin(request: Request):
        check_request_origin(request)
        return {"allowed": True}

    return app


async def test_dependency_rereads_account_and_isolates_context(auth_env):
    app = dependency_app()
    assert current_user.get() is None
    with pytest.raises(FrozenInstanceError):
        UserContext(1, "alice", "user_001").business_user_id = "user_002"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/identity", headers={"Authorization": "Bearer " + issue_token(i)}) for i in (1, 2)))
        assert [r.json()["business_user_id"] for r in responses] == ["user_001", "user_002"]
        assert current_user.get() is None
        client.cookies.set(COOKIE_NAME, issue_token(1))
        assert (await client.get("/failure")).status_code == 409
        assert current_user.get() is None
        for name in ("user_id", "business_user_id"):
            assert (await client.get("/identity", params={name: "user_002"})).status_code == 400
        app.state.platform_users.rows[1]["business_user_id"] = "user_changed_in_database"
        assert (await client.get("/identity")).json()["business_user_id"] == "user_changed_in_database"
        app.state.platform_users.rows[1]["status"] = "disabled"
        assert (await client.get("/identity")).status_code == 401
        app.state.platform_users.rows.clear()
        assert (await client.get("/identity")).status_code == 401


async def test_missing_invalid_and_conflicting_authentication(auth_env):
    app = dependency_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/identity")).status_code == 401
        assert (await client.get("/identity", headers={"Authorization": "Bearer malformed"})).status_code == 401
        expired = pyjwt.encode({"sub": "1", "iat": int(time.time()) - 600,
            "exp": int(time.time()) - 300, "iss": ISSUER, "jti": uuid.uuid4().hex},
            os.environ["AUTH_JWT_SECRET"], algorithm="HS256")
        assert (await client.get("/identity", headers={"Authorization": "Bearer " + expired})).status_code == 401
        client.cookies.set(COOKIE_NAME, issue_token(1))
        assert (await client.get("/identity", headers={"Authorization": "Basic something"})).status_code == 401
        assert (await client.get("/identity", headers={"Authorization": "Bearer " + issue_token(2)})).status_code == 401


async def test_cookie_csrf_same_origin_exact_allowlist_and_bearer(auth_env, monkeypatch):
    app = dependency_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(COOKIE_NAME, issue_token(1))
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.example"},
                        {"Origin": "http://test.evil.example"}, {"Origin": "http://test/path"},
                        {"Origin": "https://evil.example", "Referer": "http://test/page"}):
            assert (await client.post("/identity", headers=headers)).status_code == 403
        assert (await client.post("/identity", headers={"Origin": "http://test"})).status_code == 200
        assert (await client.post("/identity", headers={"Referer": "http://test/some/page?ok=1"})).status_code == 200
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://client.example:8443")
        assert (await client.post("/identity", headers={"Origin": "https://client.example:8443"})).status_code == 200
        assert (await client.post("/identity", headers={"Origin": "https://client.example"})).status_code == 403
        assert (await client.post("/login-origin")).status_code == 403
        client.cookies.clear()
        assert (await client.post("/identity", headers={"Authorization": "Bearer " + issue_token(1)})).status_code == 200
    for invalid in ("*", "https://*.example", "https://example/path", "https://name:password@example"):
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", invalid)
        with pytest.raises(ValueError):
            cors_allowed_origins()


async def test_real_api_login_me_logout_and_disabled_account(auth_env, monkeypatch):
    from api import main as api
    password = secrets.token_urlsafe(24)
    account = {"id": 31, "username": "auth-test", "business_user_id": "user_001",
               "status": "active", "password_hash": hash_password(password)}
    users = MemoryUsers([account])
    monkeypatch.setattr(api.app.state, "platform_users", users, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                headers={"Origin": "http://test"}) as client:
        for username, supplied in [("unknown-account", password), (account["username"], secrets.token_urlsafe(24))]:
            bad = await client.post("/api/auth/login", json={"username": username, "password": supplied})
            assert bad.status_code == 401
            assert "set-cookie" not in bad.headers
        response = await client.post("/api/auth/login", json={"username": account["username"], "password": password})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert set(response.json()["user"]) == {"account_id", "username"}
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/api/auth/me")).json() == {"account_id": 31, "username": "auth-test"}
        account["status"] = "disabled"
        assert (await client.get("/api/auth/me")).status_code == 401
        assert (await client.post("/api/auth/login", json={"username": account["username"], "password": password})).status_code == 401
        account["status"] = "active"
        assert (await client.post("/api/auth/logout")).status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 401
        assert COOKIE_NAME not in client.cookies


async def test_unauthenticated_customer_api_surface_is_closed(auth_env, monkeypatch):
    from api import main as api
    sid = "unauth-test-" + uuid.uuid4().hex
    # A regression must fail without creating approvals in the application's real SQLite.
    monkeypatch.setattr(api, "approval_service", Mock())
    routes = [
        ("POST", "/api/chat", {"message": "hello"}),
        ("GET", f"/api/history/{sid}", None), ("DELETE", f"/api/history/{sid}", None),
        ("GET", f"/api/checkpoints/{sid}", None), ("POST", f"/api/checkpoints/{sid}/resume", {}),
        ("GET", "/api/sessions", None), ("POST", "/api/sessions", None),
        ("GET", f"/api/sessions/{sid}", None), ("DELETE", f"/api/sessions/{sid}", None),
        ("GET", "/api/demo/orders", None), ("GET", "/api/tools", None),
        ("POST", "/api/tools/call", {"name": "order_query"}),
        ("POST", "/api/tools/execute", {"name": "order_query"}),
        ("POST", "/api/approvals", {"tool_name": "refund_create"}),
        ("GET", f"/api/approvals/{sid}", None),
        ("POST", f"/api/approvals/{sid}/approve", {"decided_by": "untrusted"}),
        ("POST", f"/api/approvals/{sid}/reject", {"decided_by": "untrusted"}),
        ("GET", "/api/metrics", None), ("GET", "/api/metrics/runtime", None),
        ("GET", "/api/auth/me", None),
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                headers={"Origin": "http://test"}) as client:
        for method, path, body in routes:
            response = await client.request(method, path, json=body)
            assert response.status_code == 401, (method, path, response.status_code)
            assert response.json() == {"detail": "authentication required"}
    assert not api.approval_service.mock_calls


async def test_login_schema_errors_never_echo_password_input(auth_env):
    from api import main as api
    marker = secrets.token_urlsafe(32)
    invalid_bodies = [
        {"username": "auth-test", "password": {"secret": marker}},
        {"username": "auth-test", "password": marker * 30},
        {"username": "auth-test", "password": marker, "password_confirmation": marker},
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                headers={"Origin": "http://test"}) as client:
        for body in invalid_bodies:
            response = await client.post("/api/auth/login", json=body)
            assert response.status_code == 422
            assert marker not in response.text
            assert all(set(error) == {"loc", "type", "msg"} for error in response.json()["detail"])


async def test_api_login_and_authenticated_cookie_writes_reject_cross_origin(auth_env, monkeypatch):
    from api import main as api
    users = MemoryUsers([{"id": 47, "username": "csrf-test", "business_user_id": "user_002", "status": "active"}])
    users.by_username = AsyncMock(side_effect=AssertionError("cross-origin login reached account lookup"))
    monkeypatch.setattr(api.app.state, "platform_users", users, raising=False)
    sessions = Mock()
    monkeypatch.setattr(api.app.state, "platform_sessions", sessions, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                headers={"Origin": "https://untrusted.example"}) as client:
        response = await client.post("/api/auth/login", json={"username": "csrf-test", "password": secrets.token_urlsafe(24)})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
        users.by_username.assert_not_awaited()
        client.cookies.set(COOKIE_NAME, issue_token(47))
        for path, body in [("/api/sessions", None), ("/api/chat", {"message": "hello"}),
                           ("/api/tools/call", {"name": "order_query"}),
                           ("/api/checkpoints/csrf-test/resume", {}), ("/api/auth/logout", None)]:
            response = await client.post(path, json=body)
            assert response.status_code == 403, path
            assert response.json() == {"detail": "request origin not allowed"}
            assert "set-cookie" not in response.headers
    assert not sessions.mock_calls


async def test_auth_mysql_failure_returns_sanitized_503(auth_env, monkeypatch, caplog):
    from api import main as api
    from platform_db import database
    marker = secrets.token_urlsafe(32)
    failure = database.pymysql.OperationalError(1045, "SELECT password_hash FROM platform_user; password=" + marker)
    monkeypatch.setattr(database.pymysql, "connect", Mock(side_effect=failure))
    db = PlatformDatabase(password=secrets.token_urlsafe(32))
    monkeypatch.setattr(api.app.state, "platform_users", Users(db), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                headers={"Origin": "http://test"}) as client:
        response = await client.post("/api/auth/login", json={"username": "auth-test", "password": secrets.token_urlsafe(24)})
        assert response.status_code == 503
        assert response.json() == {"detail": "platform database unavailable"}
        client.cookies.set(COOKIE_NAME, issue_token(47))
        response = await client.get("/api/auth/me")
        assert response.status_code == 503
        assert response.json() == {"detail": "authentication unavailable"}
        assert marker not in response.text
    assert marker not in caplog.text
    assert "SELECT password_hash" not in caplog.text


def test_bootstrap_requires_existing_business_user_without_mutating_sqlite(tmp_path):
    path = tmp_path / "business.db"
    with pytest.raises(ValueError, match="does not exist"):
        verify_business_user(str(path), "user_001")
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO users VALUES (?)", ("user_001",))
    verify_business_user(str(path), "user_001")
    with pytest.raises(ValueError, match="does not exist"):
        verify_business_user(str(path), "missing-user")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


async def test_real_mysql_account_session_restart_login_and_ownership(auth_env, monkeypatch):
    if os.getenv("SMARTCS_AUTH_MYSQL_TEST") != "1" and os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL requires SMARTCS_AUTH_MYSQL_TEST=1 or SMARTCS_CHECKPOINT_MYSQL_TEST=1")
    from dotenv import load_dotenv
    from api import main as api
    load_dotenv()
    db = PlatformDatabase.from_env()
    await db.initialize()
    users, sessions = Users(db), Sessions(db)
    prefix, password = "auth-test-" + uuid.uuid4().hex, secrets.token_urlsafe(24)
    password_hash = hash_password(password)
    owned_ids = []
    try:
        alice = await users.create(prefix + "-a", password_hash, prefix + "-business-a")
        owned_ids.append(alice["id"])
        bob = await users.create(prefix + "-b", password_hash, prefix + "-business-b")
        owned_ids.append(bob["id"])
        assert (await users.by_username(alice["username"]))["id"] == alice["id"]
        assert (await users.by_id(bob["id"]))["business_user_id"] == bob["business_user_id"]
        with pytest.raises(PlatformConflict):
            await users.create(alice["username"], password_hash, prefix + "-unused")
        with pytest.raises(PlatformConflict):
            await users.create(prefix + "-unused", password_hash, alice["business_user_id"])
        monkeypatch.setattr(api.app.state, "platform_users", users, raising=False)
        monkeypatch.setattr(api.app.state, "platform_sessions", sessions, raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test",
                                    headers={"Origin": "http://test"}) as client:
            assert (await client.post("/api/auth/login", json={"username": alice["username"], "password": password})).status_code == 200
            response = await client.post("/api/sessions")
            assert response.status_code == 200
            session_id = response.json()["session_id"]
            assert await sessions.get_owned(session_id, bob["id"]) is None
            assert not await sessions.delete(session_id, bob["id"])
            assert not await sessions.touch(session_id, bob["id"])
            assert await sessions.touch(session_id, alice["id"])
            await sessions.touch(session_id, alice["id"], title="first customer question")
            await sessions.touch(session_id, alice["id"], title="later question must not replace title")
            with pytest.raises(ValueError, match="title"):
                await sessions.touch(session_id, alice["id"], title="x" * 201)
            # New store instances simulate platform-store restart, not an in-memory substitute.
            restarted = PlatformDatabase.from_env()
            await restarted.initialize()
            monkeypatch.setattr(api.app.state, "platform_users", Users(restarted))
            monkeypatch.setattr(api.app.state, "platform_sessions", Sessions(restarted))
            client.cookies.clear()
            assert (await client.post("/api/auth/login", json={"username": alice["username"], "password": password})).status_code == 200
            listed = (await client.get("/api/sessions")).json()["sessions"]
            assert [row["session_id"] for row in listed] == [session_id]
            assert listed[0]["title"] == "first customer question"
            client.cookies.clear()
            assert (await client.post("/api/auth/login", json={"username": bob["username"], "password": password})).status_code == 200
            assert (await client.get("/api/sessions")).json()["sessions"] == []
            assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
        # A concurrent initial-request retry deduplicates only inside the same account.
        duplicate = await asyncio.gather(*(sessions.create(alice["id"], "initial", prefix) for _ in range(3)))
        assert len({row["session_id"] for row in duplicate}) == 1
        other = await sessions.create(bob["id"], "initial", prefix)
        assert other["session_id"] != duplicate[0]["session_id"]
        assert await sessions.delete(session_id, alice["id"])
        assert await sessions.get_owned(session_id, alice["id"]) is None
    finally:
        for account_id in owned_ids:
            def cleanup(_connection, cursor, account_id=account_id):
                cursor.execute("DELETE FROM conversation_session WHERE account_id=%s", (account_id,))
                cursor.execute("DELETE FROM platform_user WHERE id=%s", (account_id,))
            await db._call(cleanup)
