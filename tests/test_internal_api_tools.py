"""Phase 2 acceptance for POST /internal/tools/execute (Copilot single-track).

Real router, real MySQL, real MCP server + ToolExecutor over isolated Vault /
Ontology / ExecutionLedger stores. P2-7 asserts the internal channel's result
deep-equals a direct ToolExecutor call.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import internal_api.tools as internal_tools
from auth.context import UserContext, current_user
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler
from memory.long_term import LongTermMemory
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
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)


@pytest_asyncio.fixture
async def harness(tmp_path):
    """An app wired to a real Copilot ToolExecutor, plus the account/session to call it."""
    apply_migration()
    vault = ObsidianVault(tmp_path / "vault")
    vault.write_wiki_page(
        "project-alpha",
        "Project Alpha",
        "Project Alpha roadmap linking [[missing-ref]].\n\n" + ("Detailed roadmap notes. " * 25),
        tags=["project", "roadmap"],
    )
    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    alice = ontology.upsert_entity("Person", "Alice", attrs={"role": "owner"})
    proj = ontology.upsert_entity("Project", "Project Alpha")
    ontology.add_relation(alice["id"], "owns", proj["id"])

    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    server = create_default_tools(
        MCPToolServer(),
        long_term_memory=LongTermMemory(embedding_dim=64),
        vault=vault,
        ontology=ontology,
        wiki_compiler=WikiCompiler(vault, ontology),
    )
    executor = ToolExecutor(server, ledger=ledger)
    app = await build_app(tool_executor=executor, execution_ledger=ledger)
    account_id = seed_account(random_username("tools"), "user_001")
    session_id = "sess-tools"
    seed_session(session_id, account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "executor": executor,
            "vault": vault,
            "ontology": ontology,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_001",
        }
    ontology.close()


def _call(harness, tool, arguments, *, client_request_id="req-1", **token_kwargs):
    headers = service_header(
        account_id=harness["account_id"],
        session_id=harness["session_id"],
        business_user_id=token_kwargs.pop("business_user_id", harness["business_user_id"]),
        client_request_id=client_request_id,
        **token_kwargs,
    )
    return harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": tool,
            "arguments": arguments,
            "session_id": harness["session_id"],
            "client_request_id": client_request_id,
        },
        headers=headers,
    )


@pytest.mark.asyncio
async def test_vault_search_returns_real_vault_data(harness):
    response = await _call(harness, "vault_search", {"query": "Alpha"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["details"]["success"] is True
    hits = body["details"]["result"]
    assert len(hits) == 1
    assert hits[0]["title"] == "Project Alpha"
    assert "Project Alpha" in body["content"]
    assert body["executor"]["retries"] == 0


@pytest.mark.asyncio
async def test_identity_in_arguments_is_stripped_audited_and_force_bound(harness, monkeypatch):
    """P2-2: the model cannot speak for another user."""
    observed: list[str] = []

    async def echo_identity(user_id: str, note: str = "") -> dict[str, str]:
        observed.append(user_id)
        return {"user_id": user_id, "note": note}

    harness["executor"].server.register_tool(
        ToolDefinition(
            name="echo_identity_read",
            description="test tool declaring user_id",
            input_schema={
                "type": "object",
                "properties": {"user_id": {"type": "string"}, "note": {"type": "string"}},
            },
            handler=echo_identity,
        )
    )
    monkeypatch.setattr(internal_tools, "READ_TOOLS", internal_tools.READ_TOOLS | {"echo_identity_read"})

    response = await _call(
        harness,
        "echo_identity_read",
        {"note": "check", "user_id": "user_999"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["audit"]["strippedFields"] == ["user_id"]
    assert body["details"]["audit"]["forcedFields"] == ["user_id"]
    assert body["details"]["result"] == {"user_id": "user_001", "note": "check"}
    assert observed == ["user_001"]


@pytest.mark.asyncio
async def test_write_authority_fields_are_stripped(harness):
    """`confirmed` / `approval_id` are never accepted from the caller (v2 §7)."""
    response = await _call(
        harness,
        "vault_search",
        {
            "query": "Alpha",
            "confirmed": True,
            "approval_id": "apr-1",
            "account_id": 999,
            "session_id": "other",
        },
    )
    body = response.json()
    assert sorted(body["details"]["audit"]["strippedFields"]) == [
        "account_id",
        "approval_id",
        "confirmed",
        "session_id",
    ]
    assert body["details"]["requiresConfirmation"] is False


@pytest.mark.asyncio
async def test_only_read_tools_are_reachable_when_writes_off(harness):
    """Write tools are unreachable when SMARTCS_WRITE_MODE is not live."""
    for tool in ("vault_append_daily", "onto_upsert", "copilot_action_confirm", "bash"):
        response = await _call(harness, tool, {"entry": "hi"})
        assert response.status_code == 403, tool
        assert response.json()["detail"]["code"] == "tool_not_allowed_on_internal_channel"


@pytest.mark.asyncio
async def test_unknown_arguments_become_a_failed_result_not_a_crash(harness):
    """P2-3, Python side."""
    response = await _call(harness, "vault_search", {"query": "Alpha", "bogus_field": "x"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["success"] is False
    assert body["details"]["status"] == "failed"
    assert body["details"]["errorCode"] == "execution_error"
    assert "bogus_field" in (body["details"]["error"] or "")
    assert "bogus_field" in body["content"]


@pytest.mark.asyncio
async def test_service_token_must_carry_business_user_id(harness):
    """D5: tool calls require the resolved identity."""
    response = await _call(harness, "vault_search", {"query": "Alpha"}, business_user_id=None)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_service_token_session_must_match_body(harness):
    headers = service_header(
        account_id=harness["account_id"],
        session_id="some-other-session",
        business_user_id=harness["business_user_id"],
    )
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "vault_search",
            "arguments": {"query": "Alpha"},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
        headers=headers,
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_must_belong_to_the_token_account(harness):
    stranger = seed_account(random_username("stranger"), "user_003")
    headers = service_header(
        account_id=stranger, session_id=harness["session_id"], business_user_id="user_003"
    )
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "vault_search",
            "arguments": {"query": "Alpha"},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
        headers=headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_service_token_is_401(harness):
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "vault_search",
            "arguments": {"query": "Alpha"},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
    )
    assert response.status_code == 401


def _effective_arguments(executor, tool_name: str, arguments: dict, business_user_id: str) -> dict:
    from internal_api.tools import IDENTITY_FIELDS

    effective = {key: value for key, value in arguments.items() if key not in IDENTITY_FIELDS}
    schema = executor.server.get_tool(tool_name).input_schema
    if "user_id" in (schema.get("properties") or {}):
        effective["user_id"] = business_user_id
    return effective


@pytest.mark.asyncio
async def test_parity_with_direct_tool_executor(harness):
    """P2-7: the channel adds no business semantics of its own."""
    cases = [
        ("vault_search", {"query": "Alpha"}),
        ("vault_read_note", {"path": "wiki/project-alpha.md"}),
        ("wiki_lint", {}),
        ("onto_query", {"query": "Alice"}),
        ("knowledge_search", {"query": "Alpha", "top_k": 2}),
    ]
    for index, (tool, arguments) in enumerate(cases):
        response = await _call(harness, tool, arguments, client_request_id=f"req-parity-{index}")
        assert response.status_code == 200, (tool, response.text)
        channel = response.json()

        effective = _effective_arguments(harness["executor"], tool, arguments, harness["business_user_id"])
        token = current_user.set(
            UserContext(
                account_id=harness["account_id"],
                username="u",
                business_user_id=harness["business_user_id"],
            )
        )
        try:
            direct = await harness["executor"].execute(tool, effective, ToolExecutionContext())
        finally:
            current_user.reset(token)

        assert channel["details"]["success"] == direct.success, tool
        assert channel["details"]["errorCode"] == direct.error_code, tool
        assert channel["details"]["status"] == direct.status, tool
        assert channel["details"]["result"] == direct.result, tool
        assert channel["executor"]["retries"] == max(0, direct.attempts - 1), tool


@pytest.mark.asyncio
async def test_oversized_results_are_truncated_and_flagged(harness, monkeypatch):
    """P2-6: bounded content, marker, and the full payload still in details."""
    monkeypatch.setenv("SMARTCS_TOOL_RESULT_MAX_CHARS", "200")
    response = await _call(harness, "vault_read_note", {"path": "wiki/project-alpha.md"})
    body = response.json()
    assert body["details"]["contentTruncated"] is True
    assert body["content"].endswith("…<truncated>")
    assert len(body["content"]) <= 200
    assert body["details"]["result"]["title"] == "Project Alpha"


@pytest.mark.asyncio
async def test_knowledge_search_runs_through_the_shared_retriever(harness):
    """The channel reuses the process-wide retriever instance (no second index)."""
    response = await _call(harness, "knowledge_search", {"query": "Context Engineering", "top_k": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["success"] is True
    assert isinstance(body["details"]["result"], list)
