"""Offline acceptance for the Copilot bootstrap and Feishu webhook router."""

from __future__ import annotations

import hashlib
import json
from typing import Any
import uuid

import httpx
import pytest
from fastapi import FastAPI

from api import feishu_bot
from api.copilot_bootstrap import build_copilot_dependencies, build_obsidian_retriever
from api.feishu_bot import CANCEL_PHRASE, CONFIRM_PHRASE, router as feishu_router
from connectors import FeishuClient, GitHubClient, MailRouter
from internal_api.write_authorization import WriteAuthorizationService
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler
from memory.long_term import HashEmbeddingBackend, OpenAIEmbeddingBackend
from scripts.ingest_obsidian_vault import ingest_obsidian_vault
from tests.internal_api_helpers import TEST_DATABASE, apply_migration, platform_database

EVENTS_URL = "/api/integrations/feishu/events"
CARD_ACTION_URL = "/api/integrations/feishu/card-action"

_ENV_KEYS = (
    "OBSIDIAN_VAULT_PATH",
    "OBSIDIAN_SOURCE_VAULT_PATH",
    "OBSIDIAN_INDEX_PATH",
    "COPILOT_ONTOLOGY_DB_PATH",
    "MAIL_163_USERNAME",
    "MAIL_163_PASSWORD",
    "MAIL_163_AUTH_CODE",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "GITHUB_TOKEN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_OWNER_OPEN_ID",
    "FEISHU_CHAT_ACCOUNT_ID",
    "FEISHU_CHAT_BUSINESS_USER_ID",
    "FEISHU_CHAT_USER_TOKEN",
)


@pytest.fixture(autouse=True)
def _reset_feishu_state(monkeypatch: pytest.MonkeyPatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    feishu_bot.configure_chat_runner(None)
    feishu_bot.configure_feishu_client(None)
    feishu_bot.configure_write_authorization(None)
    feishu_bot.configure_open_id_bindings(None)
    yield
    feishu_bot.configure_chat_runner(None)
    feishu_bot.configure_feishu_client(None)
    feishu_bot.configure_write_authorization(None)
    feishu_bot.configure_open_id_bindings(None)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(feishu_router)
    return app


def _message_payload(
    text: str = "帮我看看今天的邮件",
    *,
    open_id: str = "ou_user_1",
    chat_id: str = "oc_chat_1",
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "token": "verification-token"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any], **kwargs):
    return await client.post(url, json=payload, **kwargs)


class FakeFeishuClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict[str, Any]]] = []

    def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: Any,
    ) -> dict[str, Any]:
        self.sent.append((receive_id, receive_id_type, msg_type, content))
        return {"message_id": "om_test"}


# --------------------------------------------------------------- bootstrap


def test_bootstrap_returns_all_none_when_unconfigured() -> None:
    dependencies = build_copilot_dependencies({})
    assert dependencies == {
        "vault": None,
        "ontology": None,
        "wiki_compiler": None,
        "mail_router": None,
        "github_client": None,
        "feishu_client": None,
    }


def test_bootstrap_builds_vault_ontology_and_wiki(tmp_path) -> None:
    vault_path = tmp_path / "vault"
    ontology_path = tmp_path / "onto.sqlite"
    dependencies = build_copilot_dependencies(
        {
            "OBSIDIAN_VAULT_PATH": str(vault_path),
            "COPILOT_ONTOLOGY_DB_PATH": str(ontology_path),
        }
    )
    try:
        assert isinstance(dependencies["vault"], ObsidianVault)
        assert isinstance(dependencies["ontology"], OntologyStore)
        assert isinstance(dependencies["wiki_compiler"], WikiCompiler)
        # The vault sandbox created its zones on construction.
        assert (vault_path / "wiki").is_dir()
        assert (vault_path / "notes").is_dir()
        assert ontology_path.exists()
    finally:
        dependencies["ontology"].close()


def test_bootstrap_mounts_read_only_source_vault_without_initializing_it(tmp_path) -> None:
    managed_path = tmp_path / "managed"
    source_path = tmp_path / "real_vault"
    (source_path / "nested").mkdir(parents=True)
    (source_path / "nested" / "note.md").write_text("# Note\n\nhello\n", encoding="utf-8")

    dual = build_copilot_dependencies(
        {
            "OBSIDIAN_VAULT_PATH": str(managed_path),
            "OBSIDIAN_SOURCE_VAULT_PATH": str(source_path),
        }
    )
    assert isinstance(dual["vault"], ObsidianVault)
    assert dual["vault"].source_root == source_path.resolve()
    assert dual["vault"].read_only is False
    assert isinstance(dual["wiki_compiler"], WikiCompiler)
    assert (managed_path / "wiki").is_dir()
    for zone in ("wiki", "raw", "notes", "daily"):
        assert not (source_path / zone).exists()

    ro_only = build_copilot_dependencies({"OBSIDIAN_SOURCE_VAULT_PATH": str(source_path)})
    assert isinstance(ro_only["vault"], ObsidianVault)
    assert ro_only["vault"].read_only is True
    assert ro_only["wiki_compiler"] is None


def test_build_obsidian_retriever_uses_isolated_local_index_and_ignores_rag_index_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source_vault"
    (source_path / "topics").mkdir(parents=True)
    (source_path / "topics" / "alpha.md").write_text(
        "# Private Alpha\n\nLocal obsidian secret note.\n", encoding="utf-8"
    )
    index_path = tmp_path / "vector" / "obsidian_index"
    ingest_obsidian_vault(
        source_vault_dir=source_path,
        index_path=index_path,
        embedding_backend=HashEmbeddingBackend(dimension=32),
        reset=True,
    )

    # Even when RAG_INDEX_ROOT points to an unrelated/missing production root,
    # the isolated Obsidian retriever still searches OBSIDIAN_INDEX_PATH.
    monkeypatch.setenv("RAG_INDEX_ROOT", str(tmp_path / "missing_rag_root"))
    retriever = build_obsidian_retriever(
        {"OBSIDIAN_INDEX_PATH": str(index_path)},
        embedding_backend=HashEmbeddingBackend(dimension=32),
    )
    assert retriever is not None
    assert retriever.is_artifact_mode is False
    hits = retriever.retrieve("Private Alpha", top_k=2)
    assert len(hits) >= 1
    assert hits[0].metadata["vault_path"] == "source/topics/alpha.md"

    with pytest.raises(ValueError, match="local embedding backend"):
        build_obsidian_retriever(
            {"OBSIDIAN_INDEX_PATH": str(index_path), "EMBEDDING_BACKEND": "openai"}
        )

    fake_remote = object.__new__(OpenAIEmbeddingBackend)
    with pytest.raises(ValueError, match="local embedding backend"):
        build_obsidian_retriever(
            {"OBSIDIAN_INDEX_PATH": str(index_path)},
            embedding_backend=fake_remote,
        )

    with pytest.raises(ValueError, match="local reranker"):
        build_obsidian_retriever(
            {"OBSIDIAN_INDEX_PATH": str(index_path), "RAG_RERANKER_BACKEND": "cohere_remote"},
            embedding_backend=HashEmbeddingBackend(dimension=32),
        )

    shared_faiss = tmp_path / "vector" / "faiss_index"
    with pytest.raises(ValueError, match="isolated from FAISS_INDEX_PATH"):
        build_obsidian_retriever(
            {
                "OBSIDIAN_INDEX_PATH": str(shared_faiss),
                "FAISS_INDEX_PATH": str(shared_faiss),
            },
            embedding_backend=HashEmbeddingBackend(dimension=32),
        )
    monkeypatch.setenv("RAG_INDEX_ROOT", str(tmp_path / "rag_root"))
    with pytest.raises(ValueError, match="isolated from RAG_INDEX_ROOT"):
        ingest_obsidian_vault(
            source_vault_dir=source_path,
            index_path=tmp_path / "rag_root" / "obsidian_index",
            embedding_backend=HashEmbeddingBackend(dimension=32),
        )
    with pytest.raises(ValueError, match="isolated from RAG_INDEX_ROOT"):
        build_obsidian_retriever(
            {
                "OBSIDIAN_INDEX_PATH": str(tmp_path / "rag_root" / "obsidian_index"),
                "RAG_INDEX_ROOT": str(tmp_path / "rag_root"),
            },
            embedding_backend=HashEmbeddingBackend(dimension=32),
        )


def test_bootstrap_instantiates_github_and_feishu_only_when_complete() -> None:
    partial = build_copilot_dependencies({"FEISHU_APP_ID": "cli_app"})
    assert partial["github_client"] is None
    assert partial["feishu_client"] is None

    dependencies = build_copilot_dependencies(
        {
            "GITHUB_TOKEN": "ghp_test",
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "app-secret",
        }
    )
    try:
        assert isinstance(dependencies["github_client"], GitHubClient)
        assert isinstance(dependencies["feishu_client"], FeishuClient)
    finally:
        dependencies["github_client"].close()
        dependencies["feishu_client"].close()


def test_bootstrap_builds_a_complete_mail_provider_and_skips_a_partial_one() -> None:
    dependencies = build_copilot_dependencies(
        {
            "MAIL_163_USERNAME": "user@163.com",
            "MAIL_163_PASSWORD": "auth-code",
            # A partial Gmail family must not stop the complete 163 one.
            "GMAIL_CLIENT_ID": "gmail-client",
        }
    )
    router = dependencies["mail_router"]
    assert isinstance(router, MailRouter)
    assert router.accounts == ["163"]


def test_bootstrap_skips_partial_163_credentials() -> None:
    dependencies = build_copilot_dependencies({"MAIL_163_USERNAME": "user@163.com"})
    assert dependencies["mail_router"] is None


# ------------------------------------------------------------ feishu router


async def test_url_verification_returns_the_challenge() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(
            client,
            EVENTS_URL,
            {"type": "url_verification", "challenge": "challenge-value", "token": "t"},
        )
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


async def test_invalid_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "encrypt-key")
    payload = {"type": "url_verification", "challenge": "abc"}
    raw = json.dumps(payload).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            EVENTS_URL,
            content=raw,
            headers={
                "content-type": "application/json",
                "x-lark-request-timestamp": "1700000000",
                "x-lark-request-nonce": "nonce",
                "x-lark-signature": "not-the-signature",
            },
        )
    assert response.status_code == 401


async def test_valid_signature_and_token_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    encrypt_key = "encrypt-key"
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", encrypt_key)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")
    payload = {
        "type": "url_verification",
        "challenge": "abc",
        "token": "verification-token",
    }
    raw = json.dumps(payload).encode()
    timestamp, nonce = "1700000000", "nonce"
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + raw
    ).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            EVENTS_URL,
            content=raw,
            headers={
                "content-type": "application/json",
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
                "x-lark-signature": signature,
            },
        )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc"}


async def test_wrong_verification_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "expected-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(
            client,
            EVENTS_URL,
            {"type": "url_verification", "challenge": "abc", "token": "wrong"},
        )
    assert response.status_code == 401


async def test_v2_event_header_token_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")

    async def runner(session_id: str, account_id: str, text: str) -> str:
        return f"ok:{text}"

    feishu_bot.configure_chat_runner(runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        ok_resp = await _post(client, EVENTS_URL, _message_payload("hi"))
        bad_payload = _message_payload("hi")
        bad_payload["header"]["token"] = "wrong-token"
        bad_resp = await _post(client, EVENTS_URL, bad_payload)
    assert ok_resp.status_code == 200
    assert ok_resp.json()["reply"] == "ok:hi"
    assert bad_resp.status_code == 401


async def test_text_message_runs_the_agent_and_builds_a_confirmation_card() -> None:
    calls: list[tuple[str, str, str]] = []

    async def runner(session_id: str, account_id: str, text: str) -> dict[str, Any]:
        calls.append((session_id, account_id, text))
        return {"response": "准备给 alice 发送邮件，请确认。", "pending_action_id": "pa_123"}

    feishu_bot.configure_chat_runner(runner)
    sender = FakeFeishuClient()
    feishu_bot.configure_feishu_client(sender)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(client, EVENTS_URL, _message_payload("发邮件给 alice"))

    assert response.status_code == 200
    body = response.json()
    assert calls == [("oc_chat_1", "ou_user_1", "发邮件给 alice")]
    assert body["status"] == "ok"
    assert body["session_id"] == "oc_chat_1"
    assert body["account_id"] == "ou_user_1"
    assert body["pending_action_id"] == "pa_123"
    assert body["sent"] is True

    card = body["card"]
    assert card is not None
    buttons = card["elements"][1]["actions"]
    button_texts = [button["text"]["content"] for button in buttons]
    assert button_texts == [CONFIRM_PHRASE, CANCEL_PHRASE]
    values = [button["value"] for button in buttons]
    assert values[0] == {
        "decision": "confirm",
        "session_id": "oc_chat_1",
        "pending_action_id": "pa_123",
    }
    assert values[1]["decision"] == "cancel"

    assert len(sender.sent) == 1
    receive_id, receive_id_type, msg_type, sent_card = sender.sent[0]
    assert (receive_id, receive_id_type, msg_type) == ("oc_chat_1", "chat_id", "interactive")
    assert sent_card == card


async def test_reply_without_a_pending_action_has_no_card() -> None:
    async def runner(session_id: str, account_id: str, text: str) -> str:
        return "今天没有需要处理的邮件。"

    feishu_bot.configure_chat_runner(runner)
    sender = FakeFeishuClient()
    feishu_bot.configure_feishu_client(sender)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(client, EVENTS_URL, _message_payload())

    body = response.json()
    assert body["reply"] == "今天没有需要处理的邮件。"
    assert body["pending_action_id"] is None
    assert body["card"] is None
    assert body["sent"] is False
    assert sender.sent == []


async def test_card_action_confirm_dispatches_the_confirm_phrase() -> None:
    calls: list[tuple[str, str, str]] = []

    async def runner(session_id: str, account_id: str, text: str) -> str:
        calls.append((session_id, account_id, text))
        return f"已处理：{text}"

    feishu_bot.configure_chat_runner(runner)

    payload = {
        "action": {
            "value": {
                "decision": "confirm",
                "session_id": "oc_chat_1",
                "pending_action_id": "pa_123",
            }
        },
        "open_id": "ou_user_1",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(client, CARD_ACTION_URL, payload)

    assert response.status_code == 200
    body = response.json()
    assert calls == [("oc_chat_1", "ou_user_1", CONFIRM_PHRASE)]
    assert body["decision"] == "confirm"
    assert body["phrase"] == CONFIRM_PHRASE
    assert body["pending_action_id"] == "pa_123"
    assert body["card"]["header"]["title"]["content"] == "已确认执行"
    assert body["card"]["header"]["template"] == "green"


def _card_trigger_envelope(
    decision: str,
    *,
    session_id: str,
    pending_action_id: str,
    open_id: str,
    token: str = "verification-token",
    open_chat_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt_{uuid.uuid4().hex[:8]}",
            "token": token,
            "event_type": "card.action.trigger",
        },
        "event": {
            "operator": {"open_id": open_id},
            "token": "c-one-time-action-token",
            "action": {
                "tag": "button",
                "value": {
                    "decision": decision,
                    "session_id": session_id,
                    "pending_action_id": pending_action_id,
                },
            },
            "host": "im_message",
            "context": {
                "open_message_id": "om_msg_1",
                "open_chat_id": open_chat_id if open_chat_id is not None else session_id,
            },
        },
    }


async def test_card_action_confirm_supports_real_card_trigger_envelope() -> None:
    calls: list[tuple[str, str, str]] = []

    async def runner(session_id: str, account_id: str, text: str) -> str:
        calls.append((session_id, account_id, text))
        return f"已处理：{text}"

    feishu_bot.configure_chat_runner(runner)

    payload = _card_trigger_envelope(
        "confirm",
        session_id="oc_chat_1",
        pending_action_id="pa_123",
        open_id="ou_user_1",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(client, CARD_ACTION_URL, payload)

    assert response.status_code == 200
    assert calls == [("oc_chat_1", "ou_user_1", CONFIRM_PHRASE)]
    assert response.json()["decision"] == "confirm"


async def test_card_action_cancel_updates_pending_action_without_chat_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_user_1")
    monkeypatch.setenv("FEISHU_CHAT_BUSINESS_USER_ID", "user_001")
    apply_migration()
    db = platform_database()
    store = WriteAuthorizationService(db)

    session_id = f"oc_chat_{uuid.uuid4().hex}"
    created = await store.create_pending_action(
        session_id=session_id,
        user_id="user_001",
        action_type="tool",
        payload={
            "tool": "mail_send",
            "arguments": {"account": "163", "to": "alice@example.com", "subject": "Hi", "body": "Body"},
        },
    )
    pending_id = created["pending_action_id"]
    reserved_created = await store.create_pending_action(
        session_id=session_id,
        user_id="user_001",
        action_type="tool",
        payload={
            "tool": "mail_send",
            "arguments": {"account": "163", "to": "alice@example.com", "subject": "Busy", "body": "Body"},
        },
    )
    reserved_id = reserved_created["pending_action_id"]
    await db._call(
        lambda _c, cur: cur.execute(
            "UPDATE pending_action SET operation_id='op-feishu-busy' WHERE id=%s",
            (reserved_id,),
        )
    )

    calls: list[tuple[str, str, str]] = []

    async def runner(sid: str, account_id: str, text: str) -> str:
        calls.append((sid, account_id, text))
        raise AssertionError("chat runner must not be called for cancel")

    feishu_bot.configure_chat_runner(runner)
    app = _app()
    app.state.platform_database = db

    flat_cancel_payload = {
        "token": "verification-token",
        "action": {
            "value": {
                "decision": "cancel",
                "session_id": session_id,
                "pending_action_id": pending_id,
            }
        },
        "open_id": "ou_user_1",
    }
    trigger_cancel_payload = _card_trigger_envelope(
        "cancel",
        session_id=session_id,
        pending_action_id=pending_id,
        open_id="ou_user_1",
    )

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Unauthorized clicker (different open_id) cannot change state.
            intruder = await _post(
                client,
                CARD_ACTION_URL,
                _card_trigger_envelope(
                    "cancel",
                    session_id=session_id,
                    pending_action_id=pending_id,
                    open_id="ou_intruder",
                ),
            )
            assert intruder.status_code == 403
            assert calls == []
            assert (await store.load_pending_action(pending_id))["status"] == "pending"

            # Another bound user whose business_user_id != pending.user_id also cannot cancel.
            feishu_bot.configure_open_id_bindings({"ou_user_1": "user_001", "ou_other": "user_002"})
            other_bound = await _post(
                client,
                CARD_ACTION_URL,
                _card_trigger_envelope(
                    "cancel",
                    session_id=session_id,
                    pending_action_id=pending_id,
                    open_id="ou_other",
                ),
            )
            assert other_bound.status_code == 403
            assert calls == []
            assert (await store.load_pending_action(pending_id))["status"] == "pending"
            feishu_bot.configure_open_id_bindings(None)

            # Card context open_chat_id must agree with action.value.session_id.
            ctx_mismatch = await _post(
                client,
                CARD_ACTION_URL,
                _card_trigger_envelope(
                    "cancel",
                    session_id=session_id,
                    pending_action_id=pending_id,
                    open_id="ou_user_1",
                    open_chat_id="oc_other_chat",
                ),
            )
            assert ctx_mismatch.status_code == 409
            assert calls == []
            assert (await store.load_pending_action(pending_id))["status"] == "pending"

            mismatch = await _post(
                client,
                CARD_ACTION_URL,
                {
                    "token": "verification-token",
                    "action": {
                        "value": {
                            "decision": "cancel",
                            "session_id": "oc_other",
                            "pending_action_id": pending_id,
                        }
                    },
                    "open_id": "ou_user_1",
                },
            )
            assert mismatch.status_code == 409
            assert calls == []
            still_pending = await store.load_pending_action(pending_id)
            assert still_pending is not None and still_pending["status"] == "pending"

            # Reserved action (operation_id set) cannot be cancelled.
            reserved_res = await _post(
                client,
                CARD_ACTION_URL,
                _card_trigger_envelope(
                    "cancel",
                    session_id=session_id,
                    pending_action_id=reserved_id,
                    open_id="ou_user_1",
                ),
            )
            assert reserved_res.status_code == 409
            assert (await store.load_pending_action(reserved_id))["status"] == "pending"

            # Real Feishu card.action.trigger v2.0 envelope succeeds.
            response = await _post(client, CARD_ACTION_URL, trigger_cancel_payload)
            assert response.status_code == 200, response.text
            body = response.json()
            assert calls == []
            assert body["status"] == "ok"
            assert body["decision"] == "cancel"
            assert body["phrase"] == CANCEL_PHRASE
            assert body["session_id"] == session_id
            assert body["pending_action_id"] == pending_id
            assert body["card"]["header"]["title"]["content"] == "已取消"
            assert body["card"]["header"]["template"] == "grey"

            row = await store.load_pending_action(pending_id)
            assert row is not None and row["status"] == "cancelled"

            again = await _post(client, CARD_ACTION_URL, flat_cancel_payload)
            assert again.status_code == 409
            assert calls == []

            missing = await _post(
                client,
                CARD_ACTION_URL,
                {
                    "token": "verification-token",
                    "action": {
                        "value": {
                            "decision": "cancel",
                            "session_id": session_id,
                            "pending_action_id": str(uuid.uuid4()),
                        }
                    },
                    "open_id": "ou_user_1",
                },
            )
            assert missing.status_code == 404
            assert calls == []
    finally:
        await db._call(
            lambda _c, cur: cur.execute(
                "DELETE FROM pending_action WHERE id IN (%s,%s)", (pending_id, reserved_id)
            )
        )


async def test_card_action_cancel_flat_format_succeeds_when_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_user_1")
    monkeypatch.setenv("FEISHU_CHAT_BUSINESS_USER_ID", "user_001")
    apply_migration()
    db = platform_database()
    store = WriteAuthorizationService(db)
    session_id = f"oc_flat_{uuid.uuid4().hex}"
    created = await store.create_pending_action(
        session_id=session_id,
        user_id="user_001",
        action_type="tool",
        payload={"tool": "mail_send", "arguments": {"account": "163", "to": "a@example.com", "subject": "S", "body": "B"}},
    )
    pending_id = created["pending_action_id"]
    app = _app()
    app.state.platform_database = db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await _post(
                client,
                CARD_ACTION_URL,
                {
                    "token": "verification-token",
                    "action": {
                        "value": {
                            "decision": "cancel",
                            "session_id": session_id,
                            "pending_action_id": pending_id,
                        }
                    },
                    "open_id": "ou_user_1",
                },
            )
        assert res.status_code == 200
        assert (await store.load_pending_action(pending_id))["status"] == "cancelled"
    finally:
        await db._call(
            lambda _c, cur: cur.execute("DELETE FROM pending_action WHERE id=%s", (pending_id,))
        )


async def test_card_action_cancel_rejects_unauthenticated_and_unbound_before_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCalledStore:
        async def load_pending_action(self, _pending_id: str):
            raise AssertionError("store must not be called before callback auth and binding checks")

        async def cancel_pending_action(self, *_args, **_kwargs):
            raise AssertionError("store must not be called before callback auth and binding checks")

    feishu_bot.configure_write_authorization(NeverCalledStore())  # type: ignore[arg-type]
    payload = _card_trigger_envelope(
        "cancel",
        session_id="oc_chat_1",
        pending_action_id="pa_123",
        open_id="ou_user_1",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        # 1) Neither verification token nor encrypt key configured -> 503
        unauth = await _post(client, CARD_ACTION_URL, payload)
        assert unauth.status_code == 503

        # 2) Callback authenticated, but owner open_id / business_user_id not configured -> 503
        monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")
        unbound = await _post(client, CARD_ACTION_URL, payload)
        assert unbound.status_code == 503

        # 3) Owner binding configured, but another open_id clicks -> 403 before store
        monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_user_1")
        monkeypatch.setenv("FEISHU_CHAT_BUSINESS_USER_ID", "user_001")
        wrong_actor = await _post(
            client,
            CARD_ACTION_URL,
            _card_trigger_envelope(
                "cancel",
                session_id="oc_chat_1",
                pending_action_id="pa_123",
                open_id="ou_other",
            ),
        )
        assert wrong_actor.status_code == 403


async def test_card_action_cancel_fails_explicitly_without_store_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verification-token")
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_user_1")
    monkeypatch.setenv("FEISHU_CHAT_BUSINESS_USER_ID", "user_001")
    calls: list[tuple[str, str, str]] = []

    async def runner(session_id: str, account_id: str, text: str) -> str:
        calls.append((session_id, account_id, text))
        return "should not run"

    feishu_bot.configure_chat_runner(runner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(
            client,
            CARD_ACTION_URL,
            _card_trigger_envelope(
                "cancel",
                session_id="oc_chat_1",
                pending_action_id="pa_123",
                open_id="ou_user_1",
            ),
        )
    assert response.status_code == 503
    assert calls == []


async def test_card_action_rejects_an_unknown_decision() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(
            client,
            CARD_ACTION_URL,
            {"action": {"value": {"decision": "maybe", "session_id": "s", "pending_action_id": "p"}}},
        )
    assert response.status_code == 400


async def test_card_action_cancel_signed_encrypt_key_trigger_enforces_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypt_key = "feishu-encrypt-secret"
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", encrypt_key)
    monkeypatch.setenv("FEISHU_OWNER_OPEN_ID", "ou_owner_real")
    monkeypatch.setenv("FEISHU_CHAT_BUSINESS_USER_ID", "user_001")
    apply_migration()
    db = platform_database()
    store = WriteAuthorizationService(db)
    session_id = f"oc_sig_{uuid.uuid4().hex}"
    created = await store.create_pending_action(
        session_id=session_id,
        user_id="user_001",
        action_type="tool",
        payload={"tool": "mail_send", "arguments": {"account": "163", "to": "a@example.com", "subject": "S", "body": "B"}},
    )
    pending_id = created["pending_action_id"]
    app = _app()
    app.state.platform_database = db

    def signed_headers(raw_body: bytes, sig_override: str | None = None) -> dict[str, str]:
        ts, nonce = "1700000001", "nonce-1"
        sig = sig_override or hashlib.sha256(
            ts.encode() + nonce.encode() + encrypt_key.encode() + raw_body
        ).hexdigest()
        return {
            "content-type": "application/json",
            "x-lark-request-timestamp": ts,
            "x-lark-request-nonce": nonce,
            "x-lark-signature": sig,
        }

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            bad_sig_raw = json.dumps(
                _card_trigger_envelope("cancel", session_id=session_id, pending_action_id=pending_id, open_id="ou_owner_real", token="")
            ).encode("utf-8")
            bad_sig = await client.post(CARD_ACTION_URL, content=bad_sig_raw, headers=signed_headers(bad_sig_raw, "deadbeef"))
            assert bad_sig.status_code == 401

            intruder_raw = json.dumps(
                _card_trigger_envelope("cancel", session_id=session_id, pending_action_id=pending_id, open_id="ou_stranger", token="")
            ).encode("utf-8")
            intruder = await client.post(CARD_ACTION_URL, content=intruder_raw, headers=signed_headers(intruder_raw))
            assert intruder.status_code == 403
            assert (await store.load_pending_action(pending_id))["status"] == "pending"

            owner_raw = json.dumps(
                _card_trigger_envelope("cancel", session_id=session_id, pending_action_id=pending_id, open_id="ou_owner_real", token="")
            ).encode("utf-8")
            ok = await client.post(CARD_ACTION_URL, content=owner_raw, headers=signed_headers(owner_raw))
            assert ok.status_code == 200
            assert (await store.load_pending_action(pending_id))["status"] == "cancelled"
    finally:
        await db._call(lambda _c, cur: cur.execute("DELETE FROM pending_action WHERE id=%s", (pending_id,)))
