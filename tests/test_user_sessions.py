"""Real Argon2 + JWT + MySQL isolation across sessions, chat, and Copilot tools."""
from __future__ import annotations

import os
import secrets

import httpx
import pytest

from knowledge_engine import ObsidianVault, OntologyStore
from platform_db.database import PlatformDatabase
from platform_db.sessions import Sessions
from platform_db.users import Users
from tests.app_state import patch_app_state
from tests.auth_integration_helpers import auth_test_data
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
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

    vault = ObsidianVault(tmp_path / "vault")
    vault.write_wiki_page("alpha", "Project Alpha", "Weekly progress summary", tags=["project"])
    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    ontology.upsert_entity("Project", "Project Alpha")

    deps = {
        "vault": vault,
        "ontology": ontology,
        "wiki_compiler": None,
        "mail_router": None,
        "github_client": None,
        "feishu_client": None,
    }
    server = create_default_tools(MCPToolServer(), **deps)
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    executor = ToolExecutor(server, ledger=ledger)

    transcripts: dict[str, list[dict]] = {}

    async def fake_forward_chat(*, session_id: str, client_request_id: str, message: str, **_kwargs):
        history = transcripts.setdefault(session_id, [])
        if not any(m.get("client_request_id") == client_request_id for m in history):
            history.append({"role": "user", "content": message, "client_request_id": client_request_id})
            history.append(
                {
                    "role": "assistant",
                    "content": "已为您检索 Obsidian 笔记。",
                    "client_request_id": client_request_id,
                }
            )
        return _FakeHarnessResponse(
            200,
            {
                "session_id": session_id,
                "client_request_id": client_request_id,
                "message": {"role": "assistant", "content": "已为您检索 Obsidian 笔记。"},
                "replayed": False,
                "intent_label": "vault",
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

    async with auth_test_data() as data:
        for name, value in (
            ("copilot_dependencies", deps),
            ("mcp_server", server),
            ("execution_ledger", ledger),
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
            yield api, data, alice, bob, vault
    ontology.close()


async def create_chat(client, request_id="first"):
    response = await client.post(
        "/api/chat",
        json={"message": "搜索 Project Alpha 笔记", "client_request_id": request_id},
    )
    assert response.status_code == 200
    return response.json()


async def test_real_jwt_session_ownership_blocks_all_cross_user_paths(authenticated_runtime):
    _api, _data, alice, bob, _vault = authenticated_runtime
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
            {"name": "vault_search", "session_id": sid, "arguments": {"query": "Alpha"}},
        ),
        (
            "POST",
            "/api/tools/call",
            {"name": "vault_search", "session_id": sid, "arguments": {"query": "Alpha"}},
        ),
    ]
    for method, path, body in attempts:
        response = await bob.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, (method, path, response.status_code)
        assert response.json() == {"detail": "session not found"}
    assert len((await alice.get(f"/api/history/{sid}")).json()["messages"]) == 2


async def test_real_jwt_copilot_context_and_tool_policy(authenticated_runtime):
    _api, data, alice, bob, vault = authenticated_runtime
    sid = (await create_chat(alice))["session_id"]
    owner = data.accounts[0]["business_user_id"]
    for path in ("/api/sessions", "/api/copilot/context", f"/api/history/{sid}"):
        response = await alice.get(path)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"

    read_ok = await bob.post(
        "/api/tools/execute", json={"name": "vault_search", "arguments": {"query": "Alpha"}}
    )
    assert read_ok.status_code == 200
    assert read_ok.json()["result"][0]["title"] == "Project Alpha"

    for endpoint in ("call", "execute"):
        denied = await bob.post(
            f"/api/tools/{endpoint}",
            json={
                "name": "vault_append_daily",
                "confirmed": True,
                "idempotency_key": "customer-cannot-write",
                "arguments": {"entry": "forbidden", "date_str": "2026-11-18"},
            },
        )
        assert denied.status_code == 403
        spoof = await bob.post(
            f"/api/tools/{endpoint}",
            json={"name": "vault_search", "arguments": {"query": "Alpha", "user_id": owner}},
        )
        assert spoof.status_code == 403

    assert not (vault.root / "daily" / "2026-11-18.md").exists()
    assert (await bob.get("/api/metrics")).status_code == 403
    assert set((await bob.get("/api/metrics/runtime")).json()) == {"requests", "tools", "recovery"}


async def test_real_mysql_reopen_login_ownership_and_initial_request_replay(authenticated_runtime, monkeypatch):
    api, data, alice, bob, _vault = authenticated_runtime
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
