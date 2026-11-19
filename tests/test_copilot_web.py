"""Tests for the single-track Copilot Web UI and read-only context routes."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

import api.main as api_main
from auth.context import UserContext
from knowledge_engine import ObsidianVault, OntologyStore
from tool_runtime.mcp_server import MCPToolServer, create_default_tools


@pytest.fixture
def copilot_web_runtime(tmp_path, monkeypatch):
    vault = ObsidianVault(tmp_path / "vault")
    vault.write_wiki_page(
        "project-alpha",
        "Project Alpha",
        "Summary linking [[missing-ref]].",
        tags=["project", "copilot"],
    )
    vault.append_daily("Reviewed inbox items", date_str="2026-11-18")

    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    alice = ontology.upsert_entity("Person", "Alice", attrs={"role": "owner"})
    proj = ontology.upsert_entity("Project", "Project Alpha")
    ontology.add_relation(alice["id"], "owns", proj["id"])

    deps = {
        "vault": vault,
        "ontology": ontology,
        "wiki_compiler": None,
        "mail_router": None,
        "github_client": object(),
        "feishu_client": None,
    }
    server = create_default_tools(MCPToolServer(), **deps)

    monkeypatch.setattr(api_main, "copilot_dependencies", deps)
    monkeypatch.setattr(api_main, "mcp_server", server)
    monkeypatch.setitem(
        api_main.app.dependency_overrides,
        api_main.get_current_user,
        lambda: UserContext(account_id=1, username="copilot", business_user_id="user_003"),
    )
    yield vault, ontology, alice
    ontology.close()


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_main.app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_workbench_is_single_track_copilot():
    async with await _client() as client:
        prof = await client.get("/api/ui/profile")
        assert prof.status_code == 200
        assert prof.json() == {"profile": "copilot"}
        page = await client.get("/")
        assert page.status_code == 200
        assert page.headers.get("cache-control") == "no-store"
        assert 'data-profile="copilot"' in page.text
        assert "<title>Personal Copilot · 个人受控工作台</title>" in page.text
        assert "ecommerce-context" not in page.text


@pytest.mark.asyncio
async def test_copilot_routes_require_auth():
    async with await _client() as client:
        for path in (
            "/api/copilot/context",
            "/api/copilot/vault/note?path=wiki/index.md",
            "/api/copilot/ontology/entity/missing",
        ):
            res = await client.get(path)
            assert res.status_code == 401
            assert res.json() == {"detail": "authentication required"}


@pytest.mark.asyncio
async def test_copilot_context_note_and_entity_endpoints(copilot_web_runtime, monkeypatch):
    _vault, _ontology, alice = copilot_web_runtime

    class StubDb:
        async def _call(self, fn):
            class FakeCursor:
                def execute(self, _sql, _params):
                    return None

                def fetchall(self):
                    return [
                        {
                            "id": "pa-123",
                            "session_id": "sess-1",
                            "type": "tool",
                            "payload": json.dumps(
                                {
                                    "tool": "mail_send",
                                    "arguments": {
                                        "account": "163",
                                        "to": "bob@example.com",
                                        "subject": "Weekly Sync",
                                    },
                                }
                            ),
                            "expires_at": datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc),
                        }
                    ]

            return fn(None, FakeCursor())

    monkeypatch.setattr(api_main.app.state, "platform_database", StubDb())

    async with await _client() as client:
        ctx_res = await client.get("/api/copilot/context")
        assert ctx_res.status_code == 200
        ctx = ctx_res.json()
        assert ctx["profile"] == "copilot"
        assert ctx["connectors"]["obsidian"]["enabled"] is True
        assert ctx["connectors"]["obsidian"]["zones"]["wiki"] >= 1
        assert ctx["connectors"]["obsidian"]["zones"]["daily"] == 1
        assert ctx["connectors"]["ontology"]["enabled"] is True
        assert ctx["connectors"]["ontology"]["entity_count"] == 2
        assert ctx["connectors"]["github"]["enabled"] is True
        assert ctx["connectors"]["mail"]["enabled"] is False
        assert ctx["connectors"]["feishu"]["enabled"] is False
        assert ctx["tool_summary"]["read"] >= 1
        assert ctx["tool_summary"]["write"] >= 1
        assert ctx["tool_summary"]["external_two_phase"] == 2
        assert any(t["name"] == "gh_create_issue" and t["action_tier"] == "external_two_phase" for t in ctx["tools"])
        assert any(n["path"] == "wiki/project-alpha.md" for n in ctx["notes"])
        assert any(e["name"] == "Alice" for e in ctx["entities"])
        assert len(ctx["pending_actions"]) == 1
        assert ctx["pending_actions"][0]["pending_action_id"] == "pa-123"
        assert ctx["pending_actions"][0]["tool"] == "mail_send"
        assert "to=bob@example.com" in ctx["pending_actions"][0]["target_summary"]

        note_res = await client.get("/api/copilot/vault/note", params={"path": "wiki/project-alpha.md"})
        assert note_res.status_code == 200
        note = note_res.json()
        assert note["title"] == "Project Alpha"
        assert note["zone"] == "wiki"
        assert "project" in note["tags"]
        assert "Summary linking" in note["content"]

        bad_path = await client.get("/api/copilot/vault/note", params={"path": "../outside.md"})
        assert bad_path.status_code == 400

        missing_note = await client.get("/api/copilot/vault/note", params={"path": "wiki/does-not-exist.md"})
        assert missing_note.status_code == 404

        ent_res = await client.get(f"/api/copilot/ontology/entity/{alice['id']}")
        assert ent_res.status_code == 200
        ent = ent_res.json()
        assert ent["entity"]["name"] == "Alice"
        assert len(ent["neighbors"]) == 1
        assert ent["neighbors"][0]["rel_type"] == "owns"
        assert ent["neighbors"][0]["entity"]["name"] == "Project Alpha"

        assert (await client.get("/api/copilot/ontology/entity/nonexistent")).status_code == 404
