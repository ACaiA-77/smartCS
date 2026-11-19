"""Tests for the single-track Copilot Web UI and read-only context routes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import httpx
import pytest

import api.main as api_main
from auth.context import UserContext
from internal_api.write_authorization import WriteAuthorizationService
from knowledge_engine import ObsidianVault, OntologyStore
from tests.app_state import patch_app_state
from tests.internal_api_helpers import TEST_DATABASE, apply_migration, platform_database
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
    monkeypatch.setattr(api_main, "obsidian_retriever", None)
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
        app_script_idx = page.text.index('/ui/app.js')
        lucide_idx = page.text.index('https://unpkg.com/lucide@latest')
        assert app_script_idx < lucide_idx
        assert '<script async src="https://unpkg.com/lucide@latest"' in page.text


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

        cancel_res = await client.post("/api/copilot/pending/missing/cancel")
        assert cancel_res.status_code == 401
        assert cancel_res.json() == {"detail": "authentication required"}


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


@pytest.mark.asyncio
async def test_copilot_web_source_vault_list_search_backlinks_and_rag(tmp_path, monkeypatch):
    from api.copilot_bootstrap import build_obsidian_retriever
    from memory.long_term import HashEmbeddingBackend
    from scripts.ingest_obsidian_vault import ingest_obsidian_vault

    managed = tmp_path / "managed"
    source = tmp_path / "source_vault"
    (source / "topics").mkdir(parents=True)
    (source / "topics" / "alpha.md").write_text(
        "# Alpha Concept\n\nsemantic phrase about distributed consensus.\n",
        encoding="utf-8",
    )
    (source / "topics" / "beta.md").write_text(
        "# Beta Ref\n\nReferences [[topics/alpha]] here.\n",
        encoding="utf-8",
    )
    (source / ".obsidian").mkdir()
    (source / ".obsidian" / "secret.md").write_text("# Hidden\n\nmust stay hidden", encoding="utf-8")

    index_path = tmp_path / "obsidian_index"
    ingest_obsidian_vault(
        managed,
        source_vault_dir=source,
        index_path=index_path,
        embedding_backend=HashEmbeddingBackend(dimension=32),
        reset=True,
    )
    retriever = build_obsidian_retriever(
        {"OBSIDIAN_INDEX_PATH": str(index_path)},
        embedding_backend=HashEmbeddingBackend(dimension=32),
    )
    assert retriever is not None

    vault = ObsidianVault(managed, source_root=source)
    deps = {
        "vault": vault,
        "ontology": None,
        "wiki_compiler": None,
        "mail_router": None,
        "github_client": None,
        "feishu_client": None,
    }
    server = create_default_tools(MCPToolServer(), obsidian_retriever=retriever, **deps)
    monkeypatch.setattr(api_main, "copilot_dependencies", deps)
    monkeypatch.setattr(api_main, "mcp_server", server)
    monkeypatch.setattr(api_main, "obsidian_retriever", retriever)
    monkeypatch.setitem(
        api_main.app.dependency_overrides,
        api_main.get_current_user,
        lambda: UserContext(account_id=1, username="copilot", business_user_id="user_003"),
    )

    async with await _client() as client:
        ctx = (await client.get("/api/copilot/context")).json()
        assert ctx["connectors"]["obsidian"]["source_enabled"] is True
        assert ctx["connectors"]["obsidian"]["rag_index_enabled"] is True
        assert ctx["connectors"]["obsidian"]["rag_documents"] == 2
        assert ctx["connectors"]["obsidian"]["zones"]["source"] == 2

        listed = (await client.get("/api/copilot/vault/notes", params={"zone": "source"})).json()
        assert listed["total"] == 2
        assert {item["path"] for item in listed["notes"]} == {
            "source/topics/alpha.md",
            "source/topics/beta.md",
        }

        rag_res = await client.get(
            "/api/copilot/vault/search",
            params={"query": "distributed consensus", "zone": "source", "mode": "rag"},
        )
        assert rag_res.status_code == 200
        rag_body = rag_res.json()
        assert rag_body["rag_enabled"] is True
        assert any(hit["path"] == "source/topics/alpha.md" for hit in rag_body["hits"])

        bl_res = await client.get(
            "/api/copilot/vault/backlinks",
            params={"target": "source/topics/alpha.md"},
        )
        assert bl_res.status_code == 200
        assert [item["path"] for item in bl_res.json()["backlinks"]] == ["source/topics/beta.md"]

        note_res = await client.get(
            "/api/copilot/vault/note",
            params={"path": "source/topics/alpha.md"},
        )
        assert note_res.status_code == 200
        assert [item["path"] for item in note_res.json()["backlinks"]] == ["source/topics/beta.md"]

        for hidden_path in ("source/.obsidian/secret.md", ".obsidian/secret.md", "source/../secret.md"):
            assert (
                await client.get("/api/copilot/vault/note", params={"path": hidden_path})
            ).status_code == 400
        assert (
            await client.get("/api/copilot/vault/search", params={"query": "x", "zone": "bogus"})
        ).status_code == 400
        assert (
            await client.get("/api/copilot/vault/backlinks", params={"target": "../outside"})
        ).status_code == 400


@pytest.mark.asyncio
async def test_copilot_pending_cancel_endpoint_real_mysql(copilot_web_runtime, monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    apply_migration()
    db = platform_database()
    store = WriteAuthorizationService(db)

    owner_id = f"user-cancel-{uuid.uuid4().hex}"
    other_id = f"user-other-{uuid.uuid4().hex}"
    session_id = f"sess-cancel-{uuid.uuid4().hex}"

    monkeypatch.setitem(
        api_main.app.dependency_overrides,
        api_main.get_current_user,
        lambda: UserContext(account_id=1, username="copilot", business_user_id=owner_id),
    )
    patch_app_state(monkeypatch, api_main.app, platform_database=db)

    payload = {
        "tool": "mail_send",
        "arguments": {
            "account": "163",
            "to": "bob@example.com",
            "subject": "Weekly Sync",
        },
    }
    own_id = (
        await store.create_pending_action(
            session_id=session_id,
            user_id=owner_id,
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]
    other_id_action = (
        await store.create_pending_action(
            session_id=session_id,
            user_id=other_id,
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]
    consumed_id = (
        await store.create_pending_action(
            session_id=session_id,
            user_id=owner_id,
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]
    reserved_id = (
        await store.create_pending_action(
            session_id=session_id,
            user_id=owner_id,
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]

    await db._call(
        lambda _c, cur: (
            cur.execute(
                "UPDATE pending_action SET status='consumed', operation_id='op-consumed-1' WHERE id=%s",
                (consumed_id,),
            ),
            cur.execute(
                "UPDATE pending_action SET operation_id='op-reserved-1' WHERE id=%s",
                (reserved_id,),
            ),
        )
    )

    try:
        async with await _client() as client:
            before_ctx = await client.get("/api/copilot/context")
            assert before_ctx.status_code == 200
            before_ids = [item["pending_action_id"] for item in before_ctx.json()["pending_actions"]]
            assert own_id in before_ids
            assert other_id_action not in before_ids
            assert consumed_id not in before_ids
            assert reserved_id not in before_ids

            ok_res = await client.post(f"/api/copilot/pending/{own_id}/cancel")
            assert ok_res.status_code == 200, ok_res.text
            assert ok_res.json() == {
                "pending_action_id": own_id,
                "session_id": session_id,
                "status": "cancelled",
                "cancelled": True,
            }

            after_ctx = await client.get("/api/copilot/context")
            assert after_ctx.status_code == 200
            after_ids = [item["pending_action_id"] for item in after_ctx.json()["pending_actions"]]
            assert own_id not in after_ids

            row = await store.load_pending_action(own_id)
            assert row is not None and row["status"] == "cancelled"

            again_res = await client.post(f"/api/copilot/pending/{own_id}/cancel")
            assert again_res.status_code == 409

            reserved_res = await client.post(f"/api/copilot/pending/{reserved_id}/cancel")
            assert reserved_res.status_code == 409
            reserved_row = await store.load_pending_action(reserved_id)
            assert reserved_row is not None and reserved_row["status"] == "pending"

            other_res = await client.post(f"/api/copilot/pending/{other_id_action}/cancel")
            assert other_res.status_code == 404
            other_row = await store.load_pending_action(other_id_action)
            assert other_row is not None and other_row["status"] == "pending"

            missing_res = await client.post(f"/api/copilot/pending/{uuid.uuid4()}/cancel")
            assert missing_res.status_code == 404

            consumed_res = await client.post(f"/api/copilot/pending/{consumed_id}/cancel")
            assert consumed_res.status_code == 409
            consumed_row = await store.load_pending_action(consumed_id)
            assert consumed_row is not None and consumed_row["status"] == "consumed"

            patch_app_state(monkeypatch, api_main.app, platform_database=None)
            no_db_res = await client.post(f"/api/copilot/pending/{own_id}/cancel")
            assert no_db_res.status_code == 503
    finally:
        await db._call(
            lambda _c, cur: cur.execute(
                "DELETE FROM pending_action WHERE id IN (%s,%s,%s,%s)",
                (own_id, other_id_action, consumed_id, reserved_id),
            )
        )
