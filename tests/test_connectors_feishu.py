"""Offline acceptance tests for the Feishu OpenAPI connector."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from connectors import ConnectorError, FeishuClient
from connectors.feishu import verify_feishu_signature

APP_ID = "cli_app_id"
APP_SECRET = "app-secret-value"
BASE = "https://open.feishu.cn/open-apis"


def _token_response(token: str = "tenant-token", expire: int = 7200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": token,
            "expire": expire,
        },
    )


def _client(handler: Any) -> FeishuClient:
    return FeishuClient(
        APP_ID,
        APP_SECRET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_list_chats_caches_tenant_token_and_returns_items() -> None:
    state = {"tokens": 0, "chats": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            state["tokens"] += 1
            assert json.loads(request.content) == {
                "app_id": APP_ID,
                "app_secret": APP_SECRET,
            }
            return _token_response()
        if request.url.path == "/open-apis/im/v1/chats":
            state["chats"] += 1
            assert request.headers["authorization"] == "Bearer tenant-token"
            assert request.url.params["page_size"] == "5"
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [{"chat_id": "c1"}]}}
            )
        raise AssertionError(f"unexpected request {request.url}")

    client = _client(handler)
    assert client.list_chats(page_size=5) == [{"chat_id": "c1"}]
    assert client.list_chats(page_size=5) == [{"chat_id": "c1"}]
    assert state == {"tokens": 1, "chats": 2}


def test_read_messages_passes_container_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"items": [{"message_id": "om_1", "msg_type": "text"}]},
            },
        )

    client = _client(handler)
    messages = client.read_messages("oc_chat", "chat", page_size=10)

    assert messages[0]["message_id"] == "om_1"
    assert seen[0].url.path == "/open-apis/im/v1/messages"
    assert seen[0].url.params["container_id"] == "oc_chat"
    assert seen[0].url.params["container_id_type"] == "chat"
    assert seen[0].url.params["page_size"] == "10"


def test_read_doc_returns_content_and_document_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        assert request.url.path == "/open-apis/docx/v1/documents/doc123/raw_content"
        return httpx.Response(
            200, json={"code": 0, "data": {"content": "hello doc"}}
        )

    client = _client(handler)
    document = client.read_doc("doc123")
    assert document == {"content": "hello doc", "document_id": "doc123"}


def test_send_message_serializes_content_and_uuid() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        captured["params"] = dict(request.url.params)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"code": 0, "data": {"message_id": "om_sent"}}
        )

    client = _client(handler)
    result = client.send_message(
        "oc_chat",
        "chat_id",
        "text",
        {"text": "你好"},
        uuid_key="idem-1",
    )

    assert result == {"message_id": "om_sent"}
    assert captured["params"] == {"receive_id_type": "chat_id"}
    assert captured["json"] == {
        "receive_id": "oc_chat",
        "msg_type": "text",
        "content": '{"text":"你好"}',
        "uuid": "idem-1",
    }


def test_send_message_without_uuid_omits_key() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_2"}})

    client = _client(handler)
    client.send_message("ou_user", "open_id", "text", '{"text":"hi"}')
    assert "uuid" not in captured
    assert captured["content"] == '{"text":"hi"}'


def test_api_error_code_and_message_are_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        return httpx.Response(200, json={"code": 10003, "msg": "invalid param"})

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.list_chats()
    assert "10003" in str(excinfo.value)
    assert "invalid param" in str(excinfo.value)
    assert excinfo.value.code == 10003  # type: ignore[attr-defined]


def test_app_secret_is_redacted_from_token_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, text=f"invalid app_secret={APP_SECRET} app_id={APP_ID}"
        )

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.list_chats()
    message = str(excinfo.value)
    assert APP_SECRET not in message
    assert "HTTP 400" in message


def test_token_is_refreshed_when_cached_expiry_passes() -> None:
    state = {"tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            state["tokens"] += 1
            return _token_response(f"token-{state['tokens']}")
        assert request.headers["authorization"] == f"Bearer token-{state['tokens']}"
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    client = _client(handler)
    assert client.list_chats() == []
    assert state["tokens"] == 1
    client._token_expires_at = 0.0
    assert client.list_chats() == []
    assert state["tokens"] == 2


def test_invalid_token_code_forces_one_refresh_and_retry() -> None:
    state = {"tokens": 0, "messages": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            state["tokens"] += 1
            return _token_response(f"token-{state['tokens']}")
        state["messages"] += 1
        if state["messages"] == 1:
            return httpx.Response(
                200, json={"code": 99991663, "msg": "invalid access token"}
            )
        assert request.headers["authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    client = _client(handler)
    assert client.read_messages("oc_chat") == []
    assert state == {"tokens": 2, "messages": 2}


def test_send_message_never_retries_transient_failures() -> None:
    state = {"writes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        state["writes"] += 1
        return httpx.Response(503, text="busy")

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.send_message("oc_chat", "chat_id", "text", '{"text":"hi"}')
    assert state["writes"] == 1
    assert excinfo.value.status_code == 503


def test_read_retries_transient_status_then_succeeds() -> None:
    state = {"reads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return _token_response()
        state["reads"] += 1
        if state["reads"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    client = _client(handler)
    assert client.list_chats() == []
    assert state["reads"] == 2


def test_verify_feishu_signature() -> None:
    timestamp = "1700000000"
    nonce = "nonce-123"
    encrypt_key = "encrypt-key"
    body = b'{"challenge":"abc","type":"url_verification"}'
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()

    assert verify_feishu_signature(
        timestamp, nonce, encrypt_key, body, signature
    )
    assert verify_feishu_signature(
        timestamp, nonce, encrypt_key, body, signature.upper()
    )
    assert verify_feishu_signature(
        timestamp,
        nonce,
        encrypt_key,
        body.decode("utf-8"),
        signature,
    )
    assert not verify_feishu_signature(
        timestamp, nonce, encrypt_key, body + b"tampered", signature
    )
    assert not verify_feishu_signature(timestamp, nonce, encrypt_key, body, "")


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.list_chats(page_size=0),
        lambda client: client.list_chats(page_size=101),
        lambda client: client.read_messages(""),
        lambda client: client.read_doc(""),
        lambda client: client.send_message("", "chat_id", "text", "{}"),
        lambda client: client.send_message("c", "chat_id", "text", "{}", uuid_key="x" * 51),
        lambda client: client.send_message("c", "chat_id", "text", 123),
        lambda client: client.send_message("c", "chat_id", "text", ""),
    ],
)
def test_input_validation(operation: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("validation must happen before any request")

    client = _client(handler)
    with pytest.raises(ValueError):
        operation(client)


def test_empty_credentials_are_rejected() -> None:
    with pytest.raises(ValueError):
        FeishuClient("", "secret")
    with pytest.raises(ValueError):
        FeishuClient("app", "")
