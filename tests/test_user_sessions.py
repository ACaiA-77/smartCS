"""Real Argon2 + JWT + MySQL customer isolation; only the LLM is deterministic."""
from __future__ import annotations

import os
import secrets

import httpx
import pytest

from platform_db.database import PlatformDatabase
from platform_db.sessions import Sessions
from platform_db.users import Users
from tests.auth_integration_helpers import auth_test_data
from tests.test_checkpoint import runtime


@pytest.fixture
async def authenticated_runtime(tmp_path, monkeypatch):
    if os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL disabled; set SMARTCS_CHECKPOINT_MYSQL_TEST=1 explicitly")
    from api import main as api
    from mcp.order_repository import OrderRepository
    monkeypatch.setenv("AUTH_JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")
    repository = OrderRepository(str(tmp_path / "orders.db"))
    async with auth_test_data(repository) as data:
        agent, _, _, executor, repository = runtime(tmp_path, data.checkpoint)
        for name, value in (("checkpoint_store", data.checkpoint), ("chat_orchestrator", agent),
                            ("session_store", agent.session_store), ("order_repository", repository),
                            ("mcp_server", executor.server), ("tool_executor", executor)):
            monkeypatch.setattr(api, name, value)
        monkeypatch.setattr(api.app.state, "platform_users", Users(data.database), raising=False)
        monkeypatch.setattr(api.app.state, "platform_sessions", Sessions(data.database), raising=False)
        assert api.get_current_user not in api.app.dependency_overrides
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": "http://test"}) as alice, \
                httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Origin": "http://test"}) as bob:
            for index, client in enumerate((alice, bob)):
                result = await client.post("/api/auth/login", json=data.credentials(index))
                assert result.status_code == 200
                assert "HttpOnly" in result.headers["set-cookie"]
                assert client.cookies.get("smartcs_auth")
            yield api, data, alice, bob, repository


async def create_chat(client, request_id="first"):
    response = await client.post("/api/chat", json={"message": "查询订单 ORD-20260801-0002",
                                                   "client_request_id": request_id})
    assert response.status_code == 200
    return response.json()


async def test_real_jwt_session_ownership_blocks_all_cross_user_paths(authenticated_runtime):
    _api, data, alice, bob, _repository = authenticated_runtime
    created = await create_chat(alice)
    sid = created["session_id"]
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2
    assert {s["session_id"] for s in (await alice.get("/api/sessions")).json()["sessions"]} == {sid}
    assert (await bob.get("/api/sessions")).json()["sessions"] == []
    attempts = [
        ("GET", f"/api/sessions/{sid}", None),
        ("GET", f"/api/history/{sid}", None),
        ("GET", f"/api/checkpoints/{sid}", None),
        ("POST", f"/api/checkpoints/{sid}/resume", {}),
        ("DELETE", f"/api/history/{sid}", None),
        ("DELETE", f"/api/sessions/{sid}", None),
        ("POST", "/api/chat", {"message": "继续", "session_id": sid}),
        ("POST", "/api/tools/execute", {"name": "order_query", "session_id": sid,
                                        "arguments": {"order_id": "ORD-20260801-0001"}}),
        ("POST", "/api/tools/call", {"name": "order_query", "session_id": sid,
                                     "arguments": {"order_id": "ORD-20260801-0001"}}),
    ]
    for method, path, body in attempts:
        response = await bob.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.status_code)
        assert response.json() == {"detail": "session not found"}
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2
    assert (await data.checkpoint.load(sid, data.accounts[0]["business_user_id"])).status == "finished"


async def test_real_jwt_identity_spoof_orders_refunds_and_tool_policy(authenticated_runtime):
    _api, data, alice, bob, repository = authenticated_runtime
    sid = (await create_chat(alice))["session_id"]
    owner = data.accounts[0]["business_user_id"]
    for path in ("/api/sessions", "/api/demo/orders", f"/api/history/{sid}", f"/api/checkpoints/{sid}"):
        response = await bob.get(path, params={"user_id": owner})
        assert response.status_code == 400
    for path, payload in (("/api/chat", {"message": "你好", "user_id": owner}),
                          (f"/api/checkpoints/{sid}/resume", {"user_id": owner})):
        assert (await bob.post(path, json=payload)).status_code == 422
    for endpoint in ("call", "execute"):
        for identity in ({"user_id": owner}, {"business_user_id": owner}):
            response = await bob.post(f"/api/tools/{endpoint}", json={"name": "order_query",
                "arguments": {"order_id": "ORD-20260801-0002", **identity}})
            assert response.status_code == 403
        hidden = await bob.post(f"/api/tools/{endpoint}", json={"name": "order_query",
            "arguments": {"order_id": "ORD-20260801-0002"}})
        assert hidden.status_code == 200 and hidden.json()["result"]["found"] is False
        assert not {"items", "payment", "shipment", "refunds"}.intersection(hidden.json()["result"])
    for index, client in enumerate((alice, bob)):
        orders = (await client.get("/api/demo/orders", params={"limit": 20})).json()["orders"]
        assert orders
        assert all(repository.get_order(o["order_id"])["user_id"] == data.accounts[index]["business_user_id"] for o in orders)
    before = repository.get_order("ORD-20260801-0002")["refunds"]
    evaluated = await bob.post("/api/tools/execute", json={"name": "refund_evaluate",
                              "arguments": {"order_id": "ORD-20260801-0002"}})
    assert evaluated.status_code == 200
    assert evaluated.json()["result"].get("eligible") is False
    for endpoint in ("call", "execute"):
        denied = await bob.post(f"/api/tools/{endpoint}", json={"name": "refund_create", "confirmed": True,
            "idempotency_key": "customer-cannot-write", "arguments": {"order_id": "ORD-20260801-0002", "reason": "cross user"}})
        assert denied.status_code == 403
    # Chat remains the only customer WRITE entry: it must reject a foreign order
    # too, including a subsequent explicit confirmation attempt.
    foreign = await bob.post("/api/chat", json={"message": "帮我退款 ORD-20260801-0002", "client_request_id": "foreign-refund"})
    assert foreign.status_code == 200
    confirmed = await bob.post("/api/chat", json={"message": "确认退款", "session_id": foreign.json()["session_id"],
                                                 "client_request_id": "foreign-confirm"})
    assert confirmed.status_code == 200
    assert "退款申请已提交" not in confirmed.json()["response"]
    assert repository.get_order("ORD-20260801-0002")["refunds"] == before
    assert (await bob.get("/api/metrics")).status_code == 403
    assert set((await bob.get("/api/metrics/runtime")).json()) == {"requests", "tools", "recovery"}


async def test_real_jwt_ticket_query_isolation_and_identity_spoof(authenticated_runtime):
    _api, data, alice, bob, repository = authenticated_runtime
    from tickets.service import TicketService
    # Internal service seeds a ticket in temporary SQLite; customer reads go
    # through the actual login cookie, MySQL account lookup and MCP boundary.
    created = TicketService(repository).create_ticket(
        client_request_id="isolated-ticket-request", user_id=data.accounts[0]["business_user_id"],
        title="private-A-title", description="private-A-description")
    assert created["success"] is True
    ticket_id = created["ticket_id"]
    for endpoint in ("call", "execute"):
        payload = {"name": "ticket_query", "arguments": {"ticket_id": ticket_id}}
        own = await alice.post(f"/api/tools/{endpoint}", json=payload)
        assert own.status_code == 200
        assert own.json()["result"]["title"] == "private-A-title"
        hidden = await bob.post(f"/api/tools/{endpoint}", json=payload)
        assert hidden.status_code == 200
        assert hidden.json()["result"] == {"success": False, "reason_code": "ticket_not_found", "ticket_id": ticket_id}
        spoof = await bob.post(f"/api/tools/{endpoint}", json={"name": "ticket_query", "arguments": {
            "ticket_id": ticket_id, "user_id": data.accounts[0]["business_user_id"]}})
        assert spoof.status_code == 403
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


async def test_real_mysql_reopen_login_ownership_and_initial_request_replay(authenticated_runtime, monkeypatch):
    api, data, alice, bob, _repository = authenticated_runtime
    first = await create_chat(alice, "lost-initial-response")
    assert await create_chat(alice, "lost-initial-response") == first
    sid = first["session_id"]
    assert len((await alice.get("/api/sessions")).json()["sessions"]) == 1
    # New DB/store objects simulate application store recreation; the HTTP probe
    # separately kills and restarts a real Uvicorn process.
    database = PlatformDatabase.from_env()
    monkeypatch.setattr(api.app.state, "platform_users", Users(database))
    monkeypatch.setattr(api.app.state, "platform_sessions", Sessions(database))
    from checkpoint.store import CheckpointStore
    monkeypatch.setattr(api, "checkpoint_store", CheckpointStore.from_env())
    assert (await alice.post("/api/auth/logout")).status_code == 200
    assert (await alice.get("/api/sessions")).status_code == 401
    assert (await alice.post("/api/auth/login", json=data.credentials(0))).status_code == 200
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2
    assert (await bob.get(f"/api/sessions/{sid}")).status_code == 404
    assert (await alice.delete(f"/api/sessions/{sid}")).status_code == 200
    assert (await alice.get("/api/sessions")).json()["sessions"] == []
    assert await data.checkpoint.load(sid, data.accounts[0]["business_user_id"]) is None
