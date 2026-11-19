"""Milestone 3 acceptance: Copilot tool surface, two-phase writes, vault RAG.

Everything here is offline: fake connectors, an in-memory platform adapter that
mirrors ``platform_db.PlatformDatabase._call``, real SQLite for the execution
ledger and ontology, and the deterministic hash embedding backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler
from memory.long_term import HashEmbeddingBackend, LongTermMemory
from policies.consent import (
    has_copilot_local_write_consent,
    is_copilot_action_confirmation,
)
from scripts.ingest_obsidian_vault import ingest_obsidian_vault
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.copilot_tools import register_copilot_tools
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor

from tests.internal_api_helpers import (
    SERVICE_SECRET,
    USER_SECRET,
    service_header,
)

# ---------------------------------------------------------------- consent rules


@pytest.mark.parametrize(
    "message",
    ["确认发送", "好的，确认执行", "确认提交", "确认创建", "确认评论", "确认写入", "确认修改"],
)
def test_copilot_action_confirmation_accepts_explicit_phrases(message: str) -> None:
    assert is_copilot_action_confirmation(message) is True


@pytest.mark.parametrize(
    "message",
    ["取消", "不发了", "先不发了", "算了", "暂不发送", "别发", "我再想想", "", "发吧"],
)
def test_copilot_action_confirmation_rejects_cancel_and_implicit(message: str) -> None:
    assert is_copilot_action_confirmation(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "帮我保存这份草稿",
        "保存草稿",
        "把今天的进展追加到日记",
        "编译一下 wiki",
        "整理成 wiki 页面",
        "更新实体 Alice",
        "给我一个修改提案",
        "帮我记录今天做的事",
    ],
)
def test_local_write_consent_accepts_explicit_write_requests(message: str) -> None:
    assert has_copilot_local_write_consent(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "怎么保存草稿？",
        "保存草稿的步骤是什么",
        "如何更新实体",
        "有哪些规则",
        "编译流程",
        "能不能更新",
        "",
        "   ",
        "今天天气不错",
    ],
)
def test_local_write_consent_rejects_consultation_and_noise(message: str) -> None:
    assert has_copilot_local_write_consent(message) is False


# ----------------------------------------------------------------- fake clients


class FakeMailProvider:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []

    def search_messages(
        self, query: str, folder: str = "INBOX", limit: int = 10
    ) -> list[dict[str, Any]]:
        return [{"id": "m1", "subject": query, "folder": folder}][:limit]

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "messages": [{"id": "m1"}]}

    def save_draft(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "status": "draft",
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc,
            "reply_to_message_id": reply_to_message_id,
        }
        self.drafts.append(record)
        return record

    def send_message(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "status": "sent",
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc,
            "reply_to_message_id": reply_to_message_id,
            "idempotency_key": idempotency_key,
        }
        self.sent.append(record)
        return record


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    def list_notifications(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "n1", "reason": "mention"}]

    def get_issue_or_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return {"owner": owner, "repo": repo, "number": number, "title": "issue"}

    def search_code(
        self, query: str, repo: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [{"path": "a.py", "query": query, "repo": repo}][:limit]

    def create_issue(
        self, owner: str, repo: str, title: str, body: str = "", labels: list[str] | None = None
    ) -> dict[str, Any]:
        record = {"owner": owner, "repo": repo, "title": title, "body": body, "labels": labels}
        self.issues.append(record)
        return record

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        record = {"owner": owner, "repo": repo, "number": number, "body": body}
        self.comments.append(record)
        return record


class FakeFeishuClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def list_chats(self, page_size: int = 20) -> list[dict[str, Any]]:
        return [{"chat_id": "c1"}]

    def read_messages(
        self, container_id: str, container_id_type: str = "chat", page_size: int = 20
    ) -> list[dict[str, Any]]:
        return [{"message_id": "m1", "container_id": container_id}]

    def read_doc(self, document_id: str) -> dict[str, Any]:
        return {"document_id": document_id, "content": "doc"}

    def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str | dict[str, Any],
        *,
        uuid_key: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "msg_type": msg_type,
            "content": content,
            "uuid_key": uuid_key,
        }
        self.sent.append(record)
        return record


# ------------------------------------------------------- in-memory platform DB


class FakeCursor:
    """Minimal SQL dispatcher covering the authorization service's statements."""

    def __init__(self, database: "FakePlatformDatabase") -> None:
        self.database = database
        self.rowcount = 0
        self._row: dict[str, Any] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
        values = tuple(params or ())
        normalized = " ".join(str(sql).split())
        upper = normalized.upper()
        self.rowcount = 0
        self._row = None

        if upper.startswith("SELECT CONTENT FROM MEMORY_SOURCE_EVENT"):
            content = self.database.events.get((values[0], values[1]))
            if content is not None:
                self._row = {"content": content}
                self.rowcount = 1
        elif upper.startswith("SELECT OPEN_WRITE_OPERATIONS FROM AGENT_RUN_RECEIPT"):
            receipt = self.database.receipts.get((values[0], values[1]))
            if receipt is not None:
                self._row = {"open_write_operations": receipt["open_write_operations"]}
                self.rowcount = 1
        elif upper.startswith("UPDATE AGENT_RUN_RECEIPT SET OPEN_WRITE_OPERATIONS"):
            receipt = self.database.receipts.get((values[1], values[2]))
            if receipt is not None:
                receipt["open_write_operations"] = json.loads(values[0])
                self.rowcount = 1
        elif upper.startswith("UPDATE PENDING_ACTION SET OPERATION_ID"):
            pending = self.database.pending.get(values[1])
            expires_at = pending["expires_at"] if pending is not None else None
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if (
                pending is not None
                and pending["session_id"] == values[2]
                and pending["type"] == "tool"
                and pending["status"] == "pending"
                and pending["operation_id"] is None
                and expires_at is not None
                and expires_at > datetime.now(timezone.utc)
            ):
                pending["operation_id"] = values[0]
                self.rowcount = 1
        elif upper.startswith("SELECT STATUS, OPERATION_ID"):
            pending = self.database.pending.get(values[0])
            if (
                pending is not None
                and pending["session_id"] == values[1]
                and pending["type"] == "tool"
            ):
                expires_at = pending["expires_at"]
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                self._row = {
                    "status": pending["status"],
                    "operation_id": pending["operation_id"],
                    "is_live": int(expires_at is not None and expires_at > datetime.now(timezone.utc)),
                }
                self.rowcount = 1
        elif upper.startswith("INSERT INTO PENDING_ACTION"):
            pending_id, session_id, user_id, action_type, payload, expires_at = values
            self.database.pending[pending_id] = {
                "id": pending_id,
                "session_id": session_id,
                "user_id": user_id,
                "type": action_type,
                "payload": json.loads(payload),
                "status": "pending",
                "operation_id": None,
                "expires_at": expires_at,
            }
            self.rowcount = 1
        elif upper.startswith("SELECT * FROM PENDING_ACTION"):
            pending = self.database.pending.get(values[0])
            if pending is not None:
                self._row = dict(pending)
                self.rowcount = 1
        elif upper.startswith("UPDATE PENDING_ACTION SET STATUS='EXPIRED'"):
            pending = self.database.pending.get(values[0])
            expires_at = pending["expires_at"] if pending is not None else None
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if (
                pending is not None
                and pending["status"] == "pending"
                and expires_at is not None
                and expires_at <= datetime.now(timezone.utc)
            ):
                pending["status"] = "expired"
                self.rowcount = 1
        elif upper.startswith("UPDATE PENDING_ACTION SET STATUS='CONSUMED'"):
            pending = self.database.pending.get(values[0])
            if (
                pending is not None
                and pending["status"] == "pending"
                and pending["operation_id"] == values[1]
            ):
                pending["status"] = "consumed"
                self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class FakePlatformDatabase:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], str] = {}
        self.receipts: dict[tuple[str, str], dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    async def _call(self, function):
        return function(None, FakeCursor(self))

    def seed_event(self, session_id: str, client_request_id: str, content: str) -> None:
        self.events[(session_id, client_request_id)] = content

    def seed_receipt(self, session_id: str, client_request_id: str) -> None:
        self.receipts[(session_id, client_request_id)] = {
            "open_write_operations": []
        }


class FakeUsers:
    def __init__(self) -> None:
        self.accounts: dict[int, dict[str, Any]] = {}

    def add(self, account_id: int, business_user_id: str, username: str) -> None:
        self.accounts[account_id] = {
            "id": account_id,
            "username": username,
            "business_user_id": business_user_id,
            "status": "active",
        }

    async def by_id(self, account_id: int) -> dict[str, Any] | None:
        return self.accounts.get(int(account_id))


class FakeSessions:
    def __init__(self) -> None:
        self.owners: dict[str, int] = {}

    def add(self, session_id: str, account_id: int) -> None:
        self.owners[session_id] = account_id

    async def get_owned(self, session_id: str, account_id: int) -> dict[str, Any] | None:
        if self.owners.get(session_id) == int(account_id):
            return {"session_id": session_id}
        return None


@dataclass
class CopilotEnv:
    vault: ObsidianVault
    ontology: OntologyStore
    compiler: WikiCompiler
    mail: FakeMailProvider
    github: FakeGitHubClient
    feishu: FakeFeishuClient
    server: MCPToolServer
    executor: ToolExecutor
    ledger: ExecutionLedger
    approvals: Any


@pytest.fixture
def copilot_env(tmp_path: Path):
    from connectors.mail import MailRouter
    from tool_runtime.approval_store import ApprovalService

    vault = ObsidianVault(tmp_path / "vault")
    ontology = OntologyStore(tmp_path / "ontology.db")
    compiler = WikiCompiler(vault, ontology)
    mail = FakeMailProvider()
    github = FakeGitHubClient()
    feishu = FakeFeishuClient()
    server = register_copilot_tools(
        MCPToolServer(),
        vault=vault,
        ontology=ontology,
        wiki_compiler=compiler,
        mail_router=MailRouter({"163": mail}),
        github_client=github,
        feishu_client=feishu,
    )
    ledger = ExecutionLedger(tmp_path / "ledger.db")
    approvals = ApprovalService(tmp_path / "ledger.db")
    executor = ToolExecutor(
        server,
        ledger=ledger,
        approval_service=approvals,
    )
    try:
        yield CopilotEnv(
            vault=vault,
            ontology=ontology,
            compiler=compiler,
            mail=mail,
            github=github,
            feishu=feishu,
            server=server,
            executor=executor,
            ledger=ledger,
            approvals=approvals,
        )
    finally:
        ontology.close()


@dataclass
class ApiEnv:
    http: AsyncClient
    app: FastAPI
    db: FakePlatformDatabase
    users: FakeUsers
    sessions: FakeSessions
    copilot: CopilotEnv


@pytest_asyncio.fixture
async def api_env(tmp_path: Path, monkeypatch, copilot_env: CopilotEnv):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "live")

    from internal_api import internal_router

    database = FakePlatformDatabase()
    users = FakeUsers()
    users.add(1, "user_a", "alice")
    users.add(2, "user_b", "bob")
    sessions = FakeSessions()
    sessions.add("sess-a", 1)
    sessions.add("sess-b", 2)

    app = FastAPI()
    app.include_router(internal_router)
    app.state.platform_database = database
    app.state.platform_users = users
    app.state.platform_sessions = sessions
    app.state.tool_executor = copilot_env.executor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield ApiEnv(
            http=http,
            app=app,
            db=database,
            users=users,
            sessions=sessions,
            copilot=copilot_env,
        )


# ------------------------------------------------------------------- tool surface


def _tool_names(server: MCPToolServer) -> set[str]:
    return {tool["name"] for tool in server.list_tools()}


def test_register_copilot_tools_registers_the_expected_surface(copilot_env: CopilotEnv) -> None:
    names = _tool_names(copilot_env.server)
    expected = {
        "vault_search",
        "vault_read_note",
        "vault_backlinks",
        "wiki_lint",
        "onto_query",
        "mail_search",
        "mail_read_thread",
        "gh_list_notifications",
        "gh_get_issue_or_pr",
        "gh_search_code",
        "feishu_list_chats",
        "feishu_read_messages",
        "feishu_read_doc",
        "vault_append_daily",
        "vault_propose_edit",
        "wiki_compile_doc",
        "onto_upsert",
        "mail_save_draft",
        "mail_send",
        "gh_create_issue",
        "gh_post_comment",
        "feishu_send_message",
    }
    assert expected <= names

    read_tool = copilot_env.server.get_tool("vault_search")
    assert read_tool is not None
    assert (read_tool.operation_type, read_tool.risk_level) == ("read", "low")
    assert read_tool.requires_confirmation is False and read_tool.retryable is True

    local_tool = copilot_env.server.get_tool("vault_append_daily")
    assert local_tool is not None
    assert (local_tool.operation_type, local_tool.risk_level) == ("write", "low")
    assert local_tool.requires_confirmation is True and local_tool.retryable is False

    for name in ("mail_send", "gh_create_issue", "gh_post_comment", "feishu_send_message"):
        tool = copilot_env.server.get_tool(name)
        assert tool is not None, name
        assert (tool.operation_type, tool.risk_level) == ("write", "high")
        assert tool.requires_confirmation is True and tool.retryable is False


def test_copilot_tools_are_opt_in_only(tmp_path: Path) -> None:
    plain = create_default_tools(MCPToolServer())
    assert "mail_send" not in _tool_names(plain)
    assert "vault_search" not in _tool_names(plain)

    vault = ObsidianVault(tmp_path / "vault")
    partial = create_default_tools(MCPToolServer(), vault=vault)
    assert "vault_search" in _tool_names(partial)
    assert "mail_send" not in _tool_names(partial)


# ------------------------------------------------------------- executor/idempotency


@pytest.mark.asyncio
async def test_unconfirmed_external_action_is_rejected(copilot_env: CopilotEnv) -> None:
    result = await copilot_env.executor.execute(
        "mail_send",
        {"account": "163", "to": ["a@example.com"], "subject": "Hi", "body": "Body"},
    )
    assert result.status == "confirmation_required"
    assert result.error_code == "confirmation_required"
    assert copilot_env.mail.sent == []


@pytest.mark.asyncio
async def test_mail_send_replays_without_calling_the_connector_twice(
    copilot_env: CopilotEnv,
) -> None:
    arguments = {
        "account": "163",
        "to": ["a@example.com"],
        "subject": "Hi",
        "body": "Body",
        "idempotency_key": "op-mail-1",
    }
    approval = copilot_env.approvals.create_request("mail_send", arguments, requested_by="tester")
    copilot_env.approvals.approve(approval.approval_id, "tester")
    context = ToolExecutionContext(
        confirmed=True,
        idempotency_key="op-mail-1",
        approval_id=approval.approval_id,
    )

    first = await copilot_env.executor.execute("mail_send", arguments, context)
    second = await copilot_env.executor.execute("mail_send", arguments, context)

    assert first.success is True and first.replayed is False
    assert second.success is True and second.replayed is True
    assert second.attempts == 0
    assert len(copilot_env.mail.sent) == 1
    assert copilot_env.mail.sent[0]["idempotency_key"] == "op-mail-1"


@pytest.mark.asyncio
async def test_feishu_send_replays_and_truncates_uuid_key(copilot_env: CopilotEnv) -> None:
    long_key = "f" * 60
    arguments = {
        "receive_id": "ou_123",
        "receive_id_type": "open_id",
        "msg_type": "text",
        "content": '{"text":"hello"}',
        "uuid_key": long_key,
    }
    approval = copilot_env.approvals.create_request(
        "feishu_send_message", arguments, requested_by="tester"
    )
    copilot_env.approvals.approve(approval.approval_id, "tester")
    context = ToolExecutionContext(
        confirmed=True,
        idempotency_key="op-feishu-1",
        approval_id=approval.approval_id,
    )

    first = await copilot_env.executor.execute("feishu_send_message", arguments, context)
    second = await copilot_env.executor.execute("feishu_send_message", arguments, context)

    assert first.success is True and second.replayed is True
    assert len(copilot_env.feishu.sent) == 1
    assert copilot_env.feishu.sent[0]["uuid_key"] == "f" * 50


@pytest.mark.asyncio
async def test_local_write_tools_execute_under_confirmation(copilot_env: CopilotEnv) -> None:
    context = ToolExecutionContext(confirmed=True, idempotency_key="op-local-1")
    appended = await copilot_env.executor.execute(
        "vault_append_daily",
        {"entry": "finished milestone 3", "date_str": "2026-01-02"},
        context,
    )
    assert appended.success is True
    assert (copilot_env.vault.root / "daily" / "2026-01-02.md").is_file()

    entity = await copilot_env.executor.execute(
        "onto_upsert",
        {"entity_type": "Person", "name": "Alice"},
        ToolExecutionContext(confirmed=True, idempotency_key="op-local-2"),
    )
    assert entity.success is True
    assert entity.result["entity_type"] == "Person"


# ------------------------------------------------------- internal two-phase flow


def _post(
    env: ApiEnv,
    tool: str,
    arguments: dict[str, Any],
    client_request_id: str,
    *,
    account_id: int = 1,
    session_id: str = "sess-a",
    business_user_id: str = "user_a",
):
    return env.http.post(
        "/internal/tools/execute",
        json={
            "tool": tool,
            "arguments": arguments,
            "session_id": session_id,
            "client_request_id": client_request_id,
        },
        headers=service_header(
            account_id=account_id,
            session_id=session_id,
            business_user_id=business_user_id,
            client_request_id=client_request_id,
        ),
    )


MAIL_ACTION = {
    "tool": "mail_send",
    "arguments": {
        "account": "163",
        "to": ["a@example.com"],
        "subject": "Quarterly report",
        "body": "Please find the report attached.",
    },
}


@pytest.mark.asyncio
async def test_prepare_then_confirm_executes_the_frozen_snapshot(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-prep", "帮我把季度报告发给 a@example.com")
    api_env.db.seed_receipt("sess-a", "req-prep")

    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep")
    prepared_body = prepared.json()
    assert prepared_body["details"]["authorized"] is True, prepared_body
    assert prepared_body["details"]["prepared"] is True
    assert prepared_body["details"]["tool"] == "mail_send"
    assert len(prepared_body["details"]["target_hash"]) == 64
    assert "Quarterly report" in prepared_body["details"]["preview"]
    pending_id = prepared_body["details"]["pending_action_id"]
    assert pending_id

    pending = api_env.db.pending[pending_id]
    assert pending["type"] == "tool"
    assert pending["status"] == "pending"
    assert pending["user_id"] == "user_a"
    assert pending["payload"] == {"tool": "mail_send", "arguments": MAIL_ACTION["arguments"]}

    api_env.db.seed_event("sess-a", "req-confirm", "确认发送")
    api_env.db.seed_receipt("sess-a", "req-confirm")

    # Turn 2 tries to replace the subject and recipient. Both must be ignored:
    # only the frozen snapshot is executable.
    confirmed = await _post(
        api_env,
        "copilot_action_confirm",
        {"pending_action_id": pending_id, "subject": "HACKED", "to": ["evil@example.com"]},
        "req-confirm",
    )
    body = confirmed.json()
    assert body["details"]["authorized"] is True, body
    assert body["details"]["executed"] is True, body

    assert len(api_env.copilot.mail.sent) == 1
    sent = api_env.copilot.mail.sent[0]
    assert sent["subject"] == "Quarterly report"
    assert sent["to"] == ["a@example.com"]
    assert sent["idempotency_key"] == body["details"]["operationId"]

    operation_id = body["details"]["operationId"]
    consumed = api_env.db.pending[pending_id]
    assert consumed["status"] == "consumed"
    assert consumed["operation_id"] == operation_id

    ledger_record = api_env.copilot.ledger.get(operation_id)
    assert ledger_record is not None
    assert ledger_record["tool_name"] == "mail_send"
    assert ledger_record["status"] == "completed"

    # The same pending action can never be confirmed twice.
    api_env.db.seed_event("sess-a", "req-confirm-2", "确认发送")
    api_env.db.seed_receipt("sess-a", "req-confirm-2")
    again = await _post(
        api_env,
        "copilot_action_confirm",
        {"pending_action_id": pending_id},
        "req-confirm-2",
    )
    again_body = again.json()
    assert again_body["details"]["authorized"] is False
    assert again_body["details"]["errorCode"] == "pending_action_not_pending"
    assert len(api_env.copilot.mail.sent) == 1


@pytest.mark.asyncio
async def test_prepare_creates_a_pending_action_only_in_live_mode(
    api_env: ApiEnv, monkeypatch
) -> None:
    api_env.db.seed_event("sess-a", "req-prep-off", "帮我把季度报告发出去")
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "off")

    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep-off")
    body = prepared.json()
    assert body["details"]["authorized"] is True
    assert body["details"]["pending_action_id"] is None
    assert api_env.db.pending == {}

    # And the external action itself is unreachable on the internal channel.
    blocked = await _post(api_env, "mail_send", MAIL_ACTION["arguments"], "req-direct")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "tool_not_allowed_on_internal_channel"


@pytest.mark.asyncio
async def test_confirm_refuses_another_users_pending_action(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-prep", "帮我把季度报告发出去")
    api_env.db.seed_receipt("sess-a", "req-prep")
    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep")
    pending_id = prepared.json()["details"]["pending_action_id"]

    api_env.db.seed_event("sess-b", "req-steal", "确认发送")
    api_env.db.seed_receipt("sess-b", "req-steal")
    stolen = await _post(
        api_env,
        "copilot_action_confirm",
        {"pending_action_id": pending_id},
        "req-steal",
        account_id=2,
        session_id="sess-b",
        business_user_id="user_b",
    )
    body = stolen.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "pending_action_session_mismatch"
    assert api_env.copilot.mail.sent == []
    assert api_env.db.pending[pending_id]["status"] == "pending"


@pytest.mark.asyncio
async def test_confirm_refuses_a_pending_action_owned_by_another_user(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-prep", "帮我把季度报告发出去")
    api_env.db.seed_receipt("sess-a", "req-prep")
    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep")
    pending_id = prepared.json()["details"]["pending_action_id"]
    # Same session, different owner: the owner check (not the session check)
    # must fail closed.
    api_env.db.pending[pending_id]["user_id"] = "user_b"

    api_env.db.seed_event("sess-a", "req-owner", "确认发送")
    api_env.db.seed_receipt("sess-a", "req-owner")
    refused = await _post(
        api_env,
        "copilot_action_confirm",
        {"pending_action_id": pending_id},
        "req-owner",
    )
    body = refused.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "pending_action_owner_mismatch"
    assert api_env.copilot.mail.sent == []


@pytest.mark.asyncio
async def test_confirm_refuses_an_expired_pending_action(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-prep", "帮我把季度报告发出去")
    api_env.db.seed_receipt("sess-a", "req-prep")
    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep")
    pending_id = prepared.json()["details"]["pending_action_id"]

    api_env.db.pending[pending_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    api_env.db.seed_event("sess-a", "req-exp", "确认发送")
    api_env.db.seed_receipt("sess-a", "req-exp")

    expired = await _post(
        api_env, "copilot_action_confirm", {"pending_action_id": pending_id}, "req-exp"
    )
    body = expired.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "pending_action_expired"
    assert api_env.copilot.mail.sent == []
    assert api_env.db.pending[pending_id]["status"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["先不发，我再想想", "帮我发出去", "取消", "算了"])
async def test_confirm_requires_an_explicit_confirmation_phrase(
    api_env: ApiEnv, message: str
) -> None:
    api_env.db.seed_event("sess-a", "req-prep", "帮我把季度报告发出去")
    api_env.db.seed_receipt("sess-a", "req-prep")
    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-prep")
    pending_id = prepared.json()["details"]["pending_action_id"]

    api_env.db.seed_event("sess-a", "req-nc", message)
    api_env.db.seed_receipt("sess-a", "req-nc")
    refused = await _post(
        api_env, "copilot_action_confirm", {"pending_action_id": pending_id}, "req-nc"
    )
    body = refused.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "explicit_confirmation_required"
    assert api_env.copilot.mail.sent == []
    assert api_env.db.pending[pending_id]["status"] == "pending"


@pytest.mark.asyncio
async def test_prepare_refuses_smuggled_authority_fields(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-smuggle", "帮我把季度报告发出去")
    smuggled = {
        "tool": "mail_send",
        "arguments": {
            "account": "163",
            "to": ["a@example.com"],
            "subject": "Quarterly report",
            "body": "Body",
            "confirmed": True,
            "user_id": "attacker",
        },
    }
    prepared = await _post(api_env, "copilot_action_prepare", smuggled, "req-smuggle")
    body = prepared.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "identity_fields_not_allowed"
    assert api_env.db.pending == {}


@pytest.mark.asyncio
async def test_prepare_rejects_unknown_actions_and_missing_arguments(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-bad", "帮我做点事")
    api_env.db.seed_receipt("sess-a", "req-bad")

    unknown = await _post(
        api_env,
        "copilot_action_prepare",
        {"tool": "refund_create", "arguments": {}},
        "req-bad",
    )
    assert unknown.json()["details"]["errorCode"] == "unsupported_copilot_action"

    incomplete = await _post(
        api_env,
        "copilot_action_prepare",
        {"tool": "mail_send", "arguments": {"account": "163"}},
        "req-bad-2",
    )
    body = incomplete.json()
    assert body["details"]["errorCode"] == "missing_action_arguments"
    assert "to" in body["content"] and "subject" in body["content"]


@pytest.mark.asyncio
async def test_github_and_feishu_external_actions_use_the_same_gate(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-gh", "帮我在仓库提一个 issue")
    api_env.db.seed_receipt("sess-a", "req-gh")
    prepared = await _post(
        api_env,
        "copilot_action_prepare",
        {
            "tool": "gh_create_issue",
            "arguments": {
                "owner": "ACaiA-77",
                "repo": "smartCS",
                "title": "Follow up",
                "body": "Details",
            },
        },
        "req-gh",
    )
    pending_id = prepared.json()["details"]["pending_action_id"]

    api_env.db.seed_event("sess-a", "req-gh-confirm", "确认创建")
    api_env.db.seed_receipt("sess-a", "req-gh-confirm")
    confirmed = await _post(
        api_env, "copilot_action_confirm", {"pending_action_id": pending_id}, "req-gh-confirm"
    )
    body = confirmed.json()

    assert body["details"]["executed"] is True
    assert api_env.copilot.github.issues == [
        {
            "owner": "ACaiA-77",
            "repo": "smartCS",
            "title": "Follow up",
            "body": "Details",
            "labels": None,
        }
    ]

    api_env.db.seed_event("sess-a", "req-fs", "帮我给群里发条消息")
    api_env.db.seed_receipt("sess-a", "req-fs")
    feishu_prepared = await _post(
        api_env,
        "copilot_action_prepare",
        {
            "tool": "feishu_send_message",
            "arguments": {
                "receive_id": "oc_123",
                "receive_id_type": "chat_id",
                "msg_type": "text",
                "content": '{"text":"hello"}',
            },
        },
        "req-fs",
    )
    feishu_pending = feishu_prepared.json()["details"]["pending_action_id"]

    api_env.db.seed_event("sess-a", "req-fs-confirm", "确认发送")
    api_env.db.seed_receipt("sess-a", "req-fs-confirm")
    feishu_confirmed = await _post(
        api_env, "copilot_action_confirm", {"pending_action_id": feishu_pending}, "req-fs-confirm"
    )
    feishu_body = feishu_confirmed.json()

    assert feishu_body["details"]["executed"] is True
    assert len(api_env.copilot.feishu.sent) == 1
    assert api_env.copilot.feishu.sent[0]["uuid_key"] == feishu_body["details"]["operationId"][:50]


@pytest.mark.asyncio
async def test_local_write_requires_same_turn_consent(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-local", "帮我保存这份草稿")
    api_env.db.seed_receipt("sess-a", "req-local")
    (api_env.copilot.vault.root / "notes" / "manual.md").write_text(
        "# Manual\n\nbody\n", encoding="utf-8"
    )

    saved = await _post(
        api_env,
        "vault_propose_edit",
        {
            "target_rel_path": "notes/manual.md",
            "rationale": "Clarify",
            "proposed_content": "new body",
        },
        "req-local",
    )
    body = saved.json()
    assert body["details"]["authorized"] is True, body
    assert body["details"]["executed"] is True
    assert api_env.copilot.vault.root / "wiki" / "proposals"

    # A consultation turn is not consent.
    api_env.db.seed_event("sess-a", "req-consult", "怎么保存草稿？")
    api_env.db.seed_receipt("sess-a", "req-consult")
    refused = await _post(
        api_env,
        "vault_append_daily",
        {"entry": "sneaky"},
        "req-consult",
    )
    refused_body = refused.json()
    assert refused_body["details"]["authorized"] is False
    assert refused_body["details"]["errorCode"] == "explicit_consent_required"


@pytest.mark.asyncio
async def test_prepare_refuses_actions_not_registered(api_env: ApiEnv) -> None:
    api_env.db.seed_event("sess-a", "req-unreg", "帮我把季度报告发出去")
    api_env.app.state.tool_executor = ToolExecutor(MCPToolServer())

    prepared = await _post(api_env, "copilot_action_prepare", MAIL_ACTION, "req-unreg")
    body = prepared.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "action_not_available"
    assert api_env.db.pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("vault_search", {"query": "Alpha", "limit": 5}),
        ("onto_query", {"text": "Alice"}),
        ("wiki_lint", {}),
        ("mail_search", {"account": "163", "query": "quarterly"}),
        ("gh_list_notifications", {"limit": 5}),
        ("feishu_list_chats", {"page_size": 5}),
    ],
)
async def test_copilot_read_tools_are_reachable_on_internal_channel(
    api_env: ApiEnv, tool: str, arguments: dict[str, Any]
) -> None:
    response = await _post(api_env, tool, {**arguments, "confirmed": True}, f"req-read-{tool}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["details"]["success"] is True
    assert body["details"]["audit"]["strippedFields"] == ["confirmed"]


# ----------------------------------------------------------- Obsidian RAG fallback


def test_ingest_obsidian_vault_builds_updates_and_skips_proposals(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    vault.archive_raw(
        "alpha",
        "Alpha",
        "Alpha raw body about quarterly planning.",
        source_uri="https://example.test/alpha",
    )
    notes = vault.root / "notes"
    (notes / "manual.md").write_text(
        "# Manual\n\nBeta note about shipping.", encoding="utf-8", newline="\n"
    )
    (vault.root / "wiki" / "proposals" / "proposal.md").write_text(
        "# Proposal\n\nmust not be indexed", encoding="utf-8", newline="\n"
    )

    index_path = tmp_path / "rag" / "obsidian_index"
    backend = HashEmbeddingBackend(dimension=32)

    first = ingest_obsidian_vault(
        vault.root,
        index_path=index_path,
        embedding_backend=backend,
        reset=True,
    )
    assert first.loaded_count >= 2
    assert first.total_documents == first.loaded_count

    metadata = json.loads(index_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    sources = {
        Path(document["metadata"]["source_path"]).relative_to(vault.root).as_posix()
        for document in metadata
    }
    assert "raw/alpha.md" in sources
    assert "notes/manual.md" in sources
    assert all("proposals" not in source for source in sources)

    memory = LongTermMemory(
        index_path=str(index_path), embedding_backend=HashEmbeddingBackend(dimension=32)
    )
    assert memory.search("alpha", top_k=3)

    second = ingest_obsidian_vault(
        vault.root, index_path=index_path, embedding_backend=HashEmbeddingBackend(dimension=32)
    )
    assert second.loaded_count == 0
    assert second.skipped_count == 2
    assert second.total_documents == first.total_documents

    (notes / "manual.md").write_text(
        "# Manual\n\nBeta note updated with new material.", encoding="utf-8", newline="\n"
    )
    third = ingest_obsidian_vault(
        vault.root, index_path=index_path, embedding_backend=HashEmbeddingBackend(dimension=32)
    )
    assert third.loaded_count >= 1
    assert third.skipped_count == 1
    assert third.total_documents == first.total_documents

    notes_only = ingest_obsidian_vault(
        vault.root,
        index_path=index_path,
        zones=("notes",),
        embedding_backend=HashEmbeddingBackend(dimension=32),
        reset=True,
    )
    assert notes_only.zones == ("notes",)
    assert notes_only.total_documents == 1


def test_ingest_obsidian_vault_rejects_unknown_zones(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ingest_obsidian_vault(tmp_path / "vault", zones=("private",))


def test_ingest_read_only_source_vault_prunes_deletions_and_isolates_mcp_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from internal_api import mcp_gateway
    from memory.long_term import OpenAIEmbeddingBackend

    managed = tmp_path / "managed"
    source = tmp_path / "source_vault"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Outside\n\nmust never enter index", encoding="utf-8")

    (source / "nested" / "sub").mkdir(parents=True)
    note_a = source / "nested" / "sub" / "a.md"
    note_b = source / "nested" / "b.md"
    note_a.write_text("# Note A\n\nprivate topic one", encoding="utf-8")
    note_b.write_text("# Note B\n\nprivate topic two", encoding="utf-8")
    (source / ".obsidian").mkdir()
    (source / ".obsidian" / "hidden.md").write_text("# Hidden\n\nmust skip", encoding="utf-8")

    index_path = tmp_path / "obsidian_index"
    backend = HashEmbeddingBackend(dimension=32)

    first = ingest_obsidian_vault(
        managed,
        source_vault_dir=source,
        index_path=index_path,
        embedding_backend=backend,
        reset=True,
    )
    assert first.loaded_count == 2
    assert first.removed_count == 0
    assert first.total_documents == 2
    assert first.zones == ("raw", "notes", "source")
    for zone in ("wiki", "raw", "notes", "daily"):
        assert not (source / zone).exists()

    metadata = json.loads(index_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    vault_paths = {doc["metadata"]["vault_path"] for doc in metadata}
    assert vault_paths == {"source/nested/sub/a.md", "source/nested/b.md"}

    # Deleting a note from source removes its chunks during incremental ingest.
    note_b.unlink()
    second = ingest_obsidian_vault(
        managed,
        source_vault_dir=source,
        index_path=index_path,
        embedding_backend=HashEmbeddingBackend(dimension=32),
    )
    assert second.removed_count == 1
    assert second.skipped_count == 1
    assert second.total_documents == 1

    # Remote embedding backends are strictly rejected for private Obsidian notes.
    fake_remote = object.__new__(OpenAIEmbeddingBackend)
    with pytest.raises(ValueError, match="local embedding backend"):
        ingest_obsidian_vault(
            managed,
            source_vault_dir=source,
            index_path=index_path,
            embedding_backend=fake_remote,
        )
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    with pytest.raises(ValueError, match="local embedding backend"):
        ingest_obsidian_vault(managed, source_vault_dir=source, index_path=index_path)
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")

    # Independent Obsidian RAG search works via vault_search even when RAG_INDEX_ROOT
    # points elsewhere, and never leaks into knowledge_search or mcp_gateway.
    from api.copilot_bootstrap import build_obsidian_retriever

    monkeypatch.setenv("RAG_INDEX_ROOT", str(tmp_path / "other_rag_root"))
    obsidian_retriever = build_obsidian_retriever(
        {"OBSIDIAN_INDEX_PATH": str(index_path)},
        embedding_backend=HashEmbeddingBackend(dimension=32),
    )
    assert obsidian_retriever is not None

    vault = ObsidianVault(managed, source_root=source)
    shared_memory = LongTermMemory(index_path=str(tmp_path / "shared_index"), embedding_dim=32)
    shared_memory.add_document("Shared enterprise policy doc", "policy.md")

    server = create_default_tools(
        MCPToolServer(),
        long_term_memory=shared_memory,
        vault=vault,
        obsidian_retriever=obsidian_retriever,
    )
    import asyncio

    vault_hits = asyncio.run(
        server.call_tool("vault_search", {"query": "private topic one", "zone": "source", "limit": 5})
    )
    assert vault_hits.success is True
    assert [hit["path"] for hit in vault_hits.result] == ["source/nested/sub/a.md"]

    shared_hits = asyncio.run(
        server.call_tool("knowledge_search", {"query": "private topic one", "top_k": 5})
    )
    assert shared_hits.success is True
    assert all("nested" not in str(hit) for hit in shared_hits.result)

    gateway = mcp_gateway.create_server(retriever=shared_memory.get_retriever(use_env=False))
    gw_result = asyncio.run(gateway.call_tool("knowledge_search", {"query": "private topic one", "top_k": 5}))
    gw_text = str(gw_result)
    assert "private topic one" not in gw_text and "a.md" not in gw_text


def test_search_obsidian_vault_preserves_negative_reranker_scores(tmp_path: Path) -> None:
    from rag.reranker import FakeReranker
    from rag.retriever import HybridRetriever
    from tool_runtime.copilot_tools import search_obsidian_vault

    source = tmp_path / "source_vault"
    source.mkdir()
    (source / "first.md").write_text("# First\n\nsemantic query token\n", encoding="utf-8")
    (source / "second.md").write_text("# Second\n\nunrelated lexical body\n", encoding="utf-8")

    index_path = tmp_path / "obsidian_index"
    backend = HashEmbeddingBackend(dimension=32)
    ingest_obsidian_vault(
        tmp_path / "managed",
        source_vault_dir=source,
        index_path=index_path,
        embedding_backend=backend,
        reset=True,
    )
    memory = LongTermMemory(index_path=str(index_path), embedding_backend=backend)
    neg_reranker = FakeReranker(
        score_fn=lambda _q, hit: -0.5 if "first.md" in str(hit.metadata.get("vault_path")) else -3.5
    )
    retriever = HybridRetriever(None, embedding_backend=backend, reranker=neg_reranker, legacy_memory=memory)
    vault = ObsidianVault(tmp_path / "managed", source_root=source)

    rag_hits = search_obsidian_vault(vault, "semantic", mode="rag", obsidian_retriever=retriever, limit=5)
    assert [hit["path"] for hit in rag_hits] == ["source/first.md", "source/second.md"]
    assert rag_hits[0]["rag_score"] == -0.5
    assert rag_hits[1]["rag_score"] == -3.5

    hybrid_hits = search_obsidian_vault(
        vault, "semantic", mode="hybrid", obsidian_retriever=retriever, limit=5
    )
    assert hybrid_hits[0]["path"] == "source/first.md"
    assert hybrid_hits[0]["match_type"] == "hybrid"
    assert hybrid_hits[0]["rag_score"] == -0.5
