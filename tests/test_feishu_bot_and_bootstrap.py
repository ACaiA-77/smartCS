"""Offline acceptance for the Copilot bootstrap and Feishu webhook router."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from api import feishu_bot
from api.copilot_bootstrap import build_copilot_dependencies, is_copilot_profile
from api.feishu_bot import CANCEL_PHRASE, CONFIRM_PHRASE, router as feishu_router
from connectors import FeishuClient, GitHubClient, MailRouter
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler

EVENTS_URL = "/api/integrations/feishu/events"
CARD_ACTION_URL = "/api/integrations/feishu/card-action"

_ENV_KEYS = (
    "SMARTCS_PROFILE",
    "OBSIDIAN_VAULT_PATH",
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
)


@pytest.fixture(autouse=True)
def _reset_feishu_state(monkeypatch: pytest.MonkeyPatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    feishu_bot.configure_chat_runner(None)
    feishu_bot.configure_feishu_client(None)
    yield
    feishu_bot.configure_chat_runner(None)
    feishu_bot.configure_feishu_client(None)


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


def test_is_copilot_profile_reads_the_profile_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_copilot_profile() is False
    monkeypatch.setenv("SMARTCS_PROFILE", "copilot")
    assert is_copilot_profile() is True
    monkeypatch.setenv("SMARTCS_PROFILE", "  COPILOT ")
    assert is_copilot_profile() is True
    monkeypatch.setenv("SMARTCS_PROFILE", "ecommerce")
    assert is_copilot_profile() is False


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


@pytest.mark.parametrize(
    ("decision", "phrase", "title"),
    [("confirm", CONFIRM_PHRASE, "已确认执行"), ("cancel", CANCEL_PHRASE, "已取消")],
)
async def test_card_action_dispatches_the_decision_phrase(
    decision: str, phrase: str, title: str
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def runner(session_id: str, account_id: str, text: str) -> str:
        calls.append((session_id, account_id, text))
        return f"已处理：{text}"

    feishu_bot.configure_chat_runner(runner)

    payload = {
        "action": {
            "value": {
                "decision": decision,
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
    assert calls == [("oc_chat_1", "ou_user_1", phrase)]
    assert body["decision"] == decision
    assert body["phrase"] == phrase
    assert body["pending_action_id"] == "pa_123"
    assert body["card"]["header"]["title"]["content"] == title


async def test_card_action_rejects_an_unknown_decision() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await _post(
            client,
            CARD_ACTION_URL,
            {"action": {"value": {"decision": "maybe", "session_id": "s", "pending_action_id": "p"}}},
        )
    assert response.status_code == 400
