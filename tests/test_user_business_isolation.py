"""Authenticated tool, ContextVar, and Copilot sandbox isolation tests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import api.main as api_main
from auth.context import UserContext, current_user
from knowledge_engine import ObsidianVault, OntologyStore, VaultSecurityError
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor


@pytest.fixture
def copilot_runtime(tmp_path):
    vault = ObsidianVault(tmp_path / "vault")
    vault.write_wiki_page("alpha", "Alpha Note", "Confidential project note.")
    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    server = create_default_tools(MCPToolServer(), vault=vault, ontology=ontology)
    executor = ToolExecutor(server, ledger=ExecutionLedger(str(tmp_path / "copilot.db")))
    yield vault, ontology, server, executor
    ontology.close()


@contextmanager
def _as_user(user_id: str):
    token = current_user.set(
        UserContext(
            account_id=int(user_id.rsplit("_", 1)[-1]),
            username=f"user-{user_id}",
            business_user_id=user_id,
        )
    )
    try:
        yield
    finally:
        current_user.reset(token)


@pytest.mark.parametrize(
    "tool_name",
    ["vault_append_daily", "vault_propose_edit", "wiki_compile_doc", "onto_upsert", "copilot_action_confirm"],
)
def test_public_tool_arguments_reject_write_tools(tool_name: str):
    with pytest.raises(HTTPException) as exc:
        api_main._public_tool_arguments(tool_name, {})
    assert exc.value.status_code == 403


@pytest.mark.parametrize("field", ["user_id", "business_user_id"])
def test_public_tool_arguments_reject_identity_override(field: str):
    with pytest.raises(HTTPException) as exc:
        api_main._public_tool_arguments("vault_search", {"query": "Alpha", field: "user_001"})
    assert exc.value.status_code == 403


def test_vault_sandbox_blocks_traversal_and_absolute_paths(copilot_runtime, tmp_path):
    vault, _, _, _ = copilot_runtime
    outside = tmp_path / "secret.md"
    outside.write_text("top-secret", encoding="utf-8")

    for bad_path in ("../secret.md", "/etc/passwd", str(outside), "wiki/../../secret.md"):
        with pytest.raises(VaultSecurityError):
            vault.read_note(bad_path)


@pytest.mark.asyncio
async def test_write_tool_without_confirmation_never_mutates_vault(copilot_runtime):
    vault, _, _, executor = copilot_runtime
    result = await executor.execute(
        "vault_append_daily",
        {"entry": "unauthorized", "date_str": "2026-11-18"},
        ToolExecutionContext(confirmed=False, idempotency_key="unconfirmed-1"),
    )
    assert result.success is False
    assert result.error_code == "confirmation_required"
    assert not (vault.root / "daily" / "2026-11-18.md").exists()


@pytest.mark.asyncio
async def test_concurrent_user_contexts_do_not_cross_contaminate():
    async def read_context(user_id: str) -> str:
        with _as_user(user_id):
            await asyncio.sleep(0)
            ctx = current_user.get()
            assert ctx is not None
            return ctx.business_user_id

    first, second = await asyncio.gather(read_context("user_001"), read_context("user_002"))
    assert first == "user_001"
    assert second == "user_002"
    assert current_user.get() is None
