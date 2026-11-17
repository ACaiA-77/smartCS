"""Real Argon2 + JWT + MySQL customer isolation across sessions, chat, and tools."""
from __future__ import annotations

import os
import secrets

import httpx
import pytest

from platform_db.database import PlatformDatabase
from platform_db.sessions import Sessions
from platform_db.users import Users
from tests.app_state import patch_app_state
from tests.auth_integration_helpers import auth_test_data
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.order_repository import OrderRepository
from tool_runtime.tool_execution import ToolExecutor


class _FakeHarnessResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
async def authenticated_runtime(tmp_path, monkeypatch):
    if os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL disabled; set SMARTCS_CHECKPOINT_MYSQL_TEST=1 explicitly")
    from api import main as api

    monkeypatch.setenv("AUTH_JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")

    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))

    transcripts: dict[str, list[dict]] = {}

    async def fake_forward_chat(*, session_id: str, client_request_id: str, message: str, **_kwargs):
        history = transcripts.setdefault(session_id, [])
        if not any(m.get("client_request_id") == client_request_id for m in history):
            history.append({"role": "user", "content": message, "client_request_id": client_request_id})
            history.append(
                {
                    "role": "assistant",
                    "content": "已为您处理查询请求。",
                    "client_request_id": client_request_id,
                }
            )
        return _FakeHarnessResponse(
            200,
            {
                "session_id": session_id,
                "client_request_id": client_request_id,
                "message": {"role": "assistant", "content": "已为您处理查询请求。"},
                "replayed": False,
                "intent_label": "order_query",
            },
        )

    async def fake_call_harness(method: str, path: str, *, session_id: str, **_kwargs):
        if method == "DELETE":
            transcripts.pop(session_id, None)
            return _FakeHarnessResponse(200, {"deleted": True})
        msgs = [
            {"role": m["role"], "content": m["content"]}
            for m in transcripts.get(session_id, [])
        ]
        return _FakeHarnessResponse(200, {"messages": msgs})

    async with auth_test_data(repository) as data:
        for name, value in (
            ("order_repository", repository),
            ("mcp_server", server),
            ("tool_executor", executor),
            ("forward_chat", fake_forward_chat),
            ("call_harness", fake_call_harness),
        ):
            monkeypatch.setattr(api, name, value)
        patch_app_state(
            monkeypatch,
            api.app,
            platform_users=Users(data.database),
            platform_sessions=Sessions(data.database),
        )
        assert api.get_current_user not in api.app.dependency_overrides
        transport = httpx.ASGITransport(app=api.app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": "http://test"}) as alice,
            httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": "http://test"}) as bob,
        ):
            for index, client in enumerate((alice, bob)):
                result = await client.post("/api/auth/login", json=data.credentials(index))
                assert result.status_code == 200
                assert "HttpOnly" in result.headers["set-cookie"]
                assert client.cookies.get("smartcs_auth")
            yield api, data, alice, bob, repository


async def create_chat(client, request_id="first"):
    response = await client.post(
        "/api/chat",
        json={"message": "查询订单 ORD-20260801-0002", "client_request_id": request_id},
    )
    assert response.status_code == 200
    return response.json()


async def test_real_jwt_session_ownership_blocks_all_cross_user_paths(authenticated_runtime):
    _api, _data, alice, bob, _repository = authenticated_runtime
    created = await create_chat(alice)
    sid = created["session_id"]
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2
    assert {s["session_id"] for s in (await alice.get("/api/sessions")).json()["sessions"]} == {sid}
    assert (await bob.get("/api/sessions")).json()["sessions"] == []
    attempts = [
        ("GET", f"/api/sessions/{sid}", None),
        ("GET", f"/api/history/{sid}", None),
        ("DELETE", f"/api/history/{sid}", None),
        ("DELETE", f"/api/sessions/{sid}", None),
        ("POST", "/api/chat", {"message": "继续", "session_id": sid}),
        ("POST", "/api/chat/stream", {"message": "继续", "session_id": sid}),
        (
            "POST",
            "/api/tools/execute",
            {"name": "order_query", "session_id": sid, "arguments": {"order_id": "ORD-20260801-0001"}},
        ),
        (
            "POST",
            "/api/tools/call",
            {"name": "order_query", "session_id": sid, "arguments": {"order_id": "ORD-20260801-0001"}},
        ),
    ]
    for method, path, body in attempts:
        response = await bob.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.status_code)
        assert response.json() == {"detail": "session not found"}
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2


async def test_real_jwt_identity_spoof_orders_refunds_and_tool_policy(authenticated_runtime):
    _api, data, alice, bob, repository = authenticated_runtime
    sid = (await create_chat(alice))["session_id"]
    owner = data.accounts[0]["business_user_id"]
    for path in ("/api/sessions", "/api/demo/orders", f"/api/history/{sid}"):
        response = await alice.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
    for index, client in enumerate((alice, bob)):
        orders = (await client.get("/api/demo/orders")).json()["orders"]
        assert orders
        assert all(
            repository.get_order(o["order_id"])["user_id"] == data.accounts[index]["business_user_id"]
            for o in orders
        )
    before = repository.get_order("ORD-20260801-0002")["refunds"]
    evaluated = await bob.post(
        "/api/tools/execute", json={"name": "refund_evaluate", "arguments": {"order_id": "ORD-20260801-0002"}}
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["result"].get("eligible") is False
    for endpoint in ("call", "execute"):
        denied = await bob.post(
            f"/api/tools/{endpoint}",
            json={
                "name": "refund_create",
                "confirmed": True,
                "idempotency_key": "customer-cannot-write",
                "arguments": {"order_id": "ORD-20260801-0002", "reason": "cross user"},
            },
        )
        assert denied.status_code == 403
        spoof = await bob.post(
            f"/api/tools/{endpoint}",
            json={"name": "order_query", "arguments": {"order_id": "ORD-20260801-0002", "user_id": owner}},
        )
        assert spoof.status_code == 403
    assert repository.get_order("ORD-20260801-0002")["refunds"] == before
    assert (await bob.get("/api/metrics")).status_code == 403
    assert set((await bob.get("/api/metrics/runtime")).json()) == {"requests", "tools", "recovery"}


async def test_real_jwt_ticket_query_isolation_and_identity_spoof(authenticated_runtime):
    _api, data, alice, bob, repository = authenticated_runtime
    from tickets.service import TicketService

    created = TicketService(repository).create_ticket(
        client_request_id="isolated-ticket-request",
        user_id=data.accounts[0]["business_user_id"],
        title="private-A-title",
        description="private-A-description",
    )
    assert created["success"] is True
    ticket_id = created["ticket_id"]
    for endpoint in ("call", "execute"):
        payload = {"name": "ticket_query", "arguments": {"ticket_id": ticket_id}}
        own = await alice.post(f"/api/tools/{endpoint}", json=payload)
        assert own.status_code == 200
        assert own.json()["result"]["title"] == "private-A-title"
        hidden = await bob.post(f"/api/tools/{endpoint}", json=payload)
        assert hidden.status_code == 200
        assert hidden.json()["result"] == {
            "success": False,
            "reason_code": "ticket_not_found",
            "ticket_id": ticket_id,
        }
        spoof = await bob.post(
            f"/api/tools/{endpoint}",
            json={"name": "ticket_query", "arguments": {"ticket_id": ticket_id, "user_id": data.accounts[0]["business_user_id"]}},
        )
        assert spoof.status_code == 403
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


async def test_real_mysql_reopen_login_ownership_and_initial_request_replay(authenticated_runtime, monkeypatch):
    api, data, alice, bob, _repository = authenticated_runtime
    first = await create_chat(alice, "lost-initial-response")
    assert await create_chat(alice, "lost-initial-response") == first
    sid = first["session_id"]
    assert len((await alice.get("/api/sessions")).json()["sessions"]) == 1
    database = PlatformDatabase.from_env()
    patch_app_state(monkeypatch, api.app, platform_users=Users(database), platform_sessions=Sessions(database))
    assert (await alice.post("/api/auth/logout")).status_code == 200
    assert (await alice.get("/api/sessions")).status_code == 401
    assert (await alice.post("/api/auth/login", json=data.credentials(0))).status_code == 200
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2
    assert (await bob.get(f"/api/sessions/{sid}")).status_code == 404
    assert (await alice.delete(f"/api/sessions/{sid}")).status_code == 200
    assert (await alice.get("/api/sessions")).json()["sessions"] == []
