"""Offline acceptance tests for the unified mail connectors."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from typing import Any

import httpx
import pytest

from connectors import (
    ConnectorConfigError,
    ConnectorError,
    GmailApiClient,
    ImapSmtpMailClient,
    MailRouter,
)
from connectors.base import canonical_payload_hash

# ----------------------------------------------------------------- fake IMAP


def _unquote(value: str) -> str:
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace('\\"', '"').replace("\\\\", "\\")


class FakeIMAP:
    """Small IMAP double covering the commands used by the client."""

    def __init__(self, mailboxes: dict[str, dict[str, bytes]] | None = None) -> None:
        self.mailboxes = mailboxes or {}
        self.selected: str | None = None
        self.readonly: bool | None = None
        self.search_calls: list[tuple[str | None, tuple[Any, ...]]] = []
        self.appended: list[dict[str, Any]] = []
        self.login_args: tuple[str, str] | None = None
        self.logged_out = False

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.login_args = (username, password)
        return ("OK", [b"LOGIN completed"])

    def select(
        self, mailbox: str, readonly: bool = False
    ) -> tuple[str, list[bytes]]:
        name = _unquote(mailbox)
        if name not in self.mailboxes:
            return ("NO", [b"mailbox not found"])
        self.selected = name
        self.readonly = readonly
        return ("OK", [str(len(self.mailboxes[name])).encode()])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        command = command.upper()
        assert self.selected is not None
        mailbox = self.mailboxes[self.selected]
        if command == "SEARCH":
            self.search_calls.append((self.selected, args))
            uids = sorted(mailbox)
            criterion = str(args[-1])
            if criterion.upper() == "ALL":
                return ("OK", [" ".join(uids).encode()])
            if len(args) > 1 and str(args[1]).upper() == "HEADER":
                header_name = str(args[2]).casefold()
                needle = _unquote(criterion).casefold()
                matched = []
                for uid in uids:
                    message = BytesParser(policy=policy.default).parsebytes(
                        mailbox[uid]
                    )
                    if needle in str(message.get(header_name, "")).casefold():
                        matched.append(uid)
                return ("OK", [" ".join(matched).encode()])
            needle = _unquote(criterion).casefold()
            matched = [
                uid
                for uid in uids
                if needle
                in mailbox[uid].decode("utf-8", "replace").casefold()
            ]
            return ("OK", [" ".join(matched).encode()])
        if command == "FETCH":
            uid = str(args[0])
            raw = mailbox[uid]
            return (
                "OK",
                [
                    (f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw),
                    b")",
                ],
            )
        raise AssertionError(f"unexpected UID command: {command} {args}")

    def append(
        self, mailbox: str, flags: str, date_time: str, message: bytes
    ) -> tuple[str, list[bytes]]:
        self.appended.append(
            {
                "mailbox": _unquote(mailbox),
                "flags": flags,
                "date_time": date_time,
                "message": message,
            }
        )
        uid = str(10 + len(self.appended))
        return ("OK", [f"[APPENDUID 42 {uid}] APPEND completed".encode()])

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("BYE", [b"logout"])


class FakeSMTP:
    """SMTP double that records sent messages."""

    def __init__(self, refused: dict[str, tuple[int, bytes]] | None = None) -> None:
        self.refused = refused or {}
        self.sent: list[Any] = []
        self.login_args: tuple[str, str] | None = None
        self.quit_called = False

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        self.login_args = (username, password)
        return (235, b"authenticated")

    def send_message(self, message: Any) -> dict[str, tuple[int, bytes]]:
        self.sent.append(message)
        return dict(self.refused)

    def quit(self) -> tuple[int, bytes]:
        self.quit_called = True
        return (221, b"bye")


def _raw_message(
    *,
    sender: str = "Alice <alice@example.com>",
    recipient: str = "me@163.com",
    subject: str = "Hello",
    body: str = "hello world",
    html: str | None = None,
    message_id: str = "<m1@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _imap_client(
    mailboxes: dict[str, dict[str, bytes]],
) -> tuple[ImapSmtpMailClient, FakeIMAP, FakeSMTP]:
    fake_imap = FakeIMAP(mailboxes)
    fake_smtp = FakeSMTP()
    client = ImapSmtpMailClient(
        "me@163.com",
        "auth-code",
        imap_factory=lambda: fake_imap,
        smtp_factory=lambda: fake_smtp,
    )
    return client, fake_imap, fake_smtp


# ---------------------------------------------------------------- IMAP tests


def test_search_messages_decodes_headers_and_returns_newest_first() -> None:
    mailboxes = {
        "INBOX": {
            "1": _raw_message(subject="First hello", body="hello one"),
            "2": _raw_message(
                sender="张三 <zhangsan@example.com>",
                subject="第二封 hello",
                body="hello two",
            ),
            "3": _raw_message(subject="Third hello", body="hello three"),
        }
    }
    client, fake_imap, _ = _imap_client(mailboxes)

    results = client.search_messages("hello", limit=2)

    assert [item["id"] for item in results] == ["3", "2"]
    assert results[0]["subject"] == "Third hello"
    assert results[1]["subject"] == "第二封 hello"
    assert results[1]["from"] == "张三 <zhangsan@example.com>"
    assert results[0]["thread_id"] == "3"
    assert fake_imap.readonly is True
    assert fake_imap.login_args == ("me@163.com", "auth-code")
    assert fake_imap.logged_out is True
    assert fake_imap.search_calls[-1][0] == "INBOX"
    assert "TEXT" in fake_imap.search_calls[-1][1]
    assert fake_imap.search_calls[-1][1][-1] == '"hello"'


def test_search_messages_empty_query_uses_all_and_validates_limit() -> None:
    client, fake_imap, _ = _imap_client(
        {"INBOX": {"1": _raw_message(body="anything")}}
    )
    assert len(client.search_messages("", limit=5)) == 1
    assert fake_imap.search_calls[-1][1] == (None, "ALL")
    with pytest.raises(ValueError):
        client.search_messages("x", limit=0)


def test_read_thread_parses_plain_and_html_bodies() -> None:
    mailboxes = {
        "INBOX": {
            "7": _raw_message(
                sender="Alice <alice@example.com>",
                subject="Re: plans",
                body="plain text body",
                html="<p>html body</p>",
                message_id="<m7@example.com>",
                in_reply_to="<m6@example.com>",
                references="<m5@example.com> <m6@example.com>",
            )
        }
    }
    client, _, _ = _imap_client(mailboxes)

    thread = client.read_thread("7")

    assert thread["thread_id"] == "7"
    assert thread["message_count"] == 1
    message = thread["messages"][0]
    assert message["subject"] == "Re: plans"
    assert message["body"].strip() == "plain text body"
    assert message["body_html"].strip() == "<p>html body</p>"
    assert message["in_reply_to"] == "<m6@example.com>"
    assert message["references"] == ["<m5@example.com>", "<m6@example.com>"]
    assert message["uid"] == "7"


def test_read_thread_missing_message_id_raises() -> None:
    client, _, _ = _imap_client({"INBOX": {}})
    with pytest.raises(ConnectorError, match="was not found"):
        client.read_thread("<missing@example.com>")


def test_save_draft_appends_rfc822_with_reply_headers() -> None:
    client, fake_imap, _ = _imap_client({"INBOX": {}})

    result = client.save_draft(
        to=["bob@example.com"],
        subject="Draft subject",
        body="draft body",
        cc="carol@example.com",
        reply_to_message_id="orig@example.com",
    )

    assert result["status"] == "draft"
    assert result["folder"] == "Drafts"
    assert result["id"] == "11"
    assert len(fake_imap.appended) == 1
    record = fake_imap.appended[0]
    assert record["mailbox"] == "Drafts"
    assert record["flags"] == r"(\Draft)"
    parsed = BytesParser(policy=policy.default).parsebytes(record["message"])
    assert parsed["To"] == "bob@example.com"
    assert parsed["Cc"] == "carol@example.com"
    assert parsed["Subject"] == "Draft subject"
    assert parsed["In-Reply-To"] == "<orig@example.com>"
    assert parsed["References"] == "<orig@example.com>"
    assert parsed["Message-ID"] == result["message_id"]


def test_send_message_carries_idempotency_and_reply_headers() -> None:
    client, fake_imap, fake_smtp = _imap_client({"INBOX": {}})

    result = client.send_message(
        to=["bob@example.com"],
        subject="Reply",
        body="body",
        cc=["carol@example.com"],
        reply_to_message_id="<orig@example.com>",
        idempotency_key="idem-1",
    )

    assert result["status"] == "sent"
    assert result["idempotency_key"] == "idem-1"
    assert result["to"] == ["bob@example.com"]
    assert len(fake_smtp.sent) == 1
    sent = fake_smtp.sent[0]
    assert sent["X-SmartCS-Idempotency-Key"] == "idem-1"
    assert sent["In-Reply-To"] == "<orig@example.com>"
    assert sent["References"] == "<orig@example.com>"
    assert fake_smtp.login_args == ("me@163.com", "auth-code")
    assert fake_smtp.quit_called is True
    expected = canonical_payload_hash(
        {
            "to": ["bob@example.com"],
            "cc": ["carol@example.com"],
            "subject": "Reply",
            "body": "body",
            "reply_to_message_id": "<orig@example.com>",
            "idempotency_key": "idem-1",
        }
    )
    assert result["payload_hash"] == expected


def test_send_message_rejects_refused_recipients() -> None:
    fake_imap = FakeIMAP({"INBOX": {}})
    fake_smtp = FakeSMTP(refused={"bob@example.com": (550, b"unknown")})
    client = ImapSmtpMailClient(
        "me@163.com",
        "secret-auth-code",
        imap_factory=lambda: fake_imap,
        smtp_factory=lambda: fake_smtp,
    )
    with pytest.raises(ConnectorError, match="refused recipients") as excinfo:
        client.send_message("bob@example.com", "s", "b")
    assert "secret-auth-code" not in str(excinfo.value)


def test_header_injection_is_rejected_before_network() -> None:
    client, fake_imap, fake_smtp = _imap_client({"INBOX": {}})
    with pytest.raises(ValueError, match="CR or LF"):
        client.send_message("bob@example.com", "Hi\r\nBcc: evil@example.com", "b")
    with pytest.raises(ValueError, match="CR or LF"):
        client.send_message(
            "bob@example.com", "Hi", "b", idempotency_key="key\ninjected"
        )
    assert fake_imap.login_args is None
    assert fake_smtp.sent == []


def test_imap_login_failure_does_not_leak_password() -> None:
    class FailingIMAP(FakeIMAP):
        def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
            raise RuntimeError(f"server rejected {password}")

        def logout(self) -> tuple[str, list[bytes]]:
            return ("BYE", [b"bye"])

    client = ImapSmtpMailClient(
        "me@163.com",
        "super-secret-auth-code",
        imap_factory=lambda: FailingIMAP(),
    )
    with pytest.raises(ConnectorError) as excinfo:
        client.search_messages("anything")
    assert "super-secret-auth-code" not in str(excinfo.value)
    assert "IMAP login failed" in str(excinfo.value)


def test_smtp_login_failure_does_not_leak_password() -> None:
    class FailingSMTP(FakeSMTP):
        def login(self, username: str, password: str) -> tuple[int, bytes]:
            raise RuntimeError(f"server rejected {password}")

    fake_imap = FakeIMAP({"INBOX": {}})
    client = ImapSmtpMailClient(
        "me@163.com",
        "super-secret-auth-code",
        imap_factory=lambda: fake_imap,
        smtp_factory=lambda: FailingSMTP(),
    )
    with pytest.raises(ConnectorError) as excinfo:
        client.send_message("bob@example.com", "subject", "body")
    assert "super-secret-auth-code" not in str(excinfo.value)
    assert "SMTP send failed" in str(excinfo.value)


# --------------------------------------------------------------- Gmail tests


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _gmail_message(
    *,
    message_id: str = "m1",
    thread_id: str = "t1",
    subject: str = "Hello",
    plain: str = "hello body",
    html: str | None = None,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": "Alice <alice@example.com>"},
        {"name": "To", "value": "me@example.com"},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 17 Nov 2026 10:00:00 +0000"},
        {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
    ]
    if html is None:
        payload: dict[str, Any] = {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(plain)},
        }
    else:
        payload = {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64(plain)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64(html)},
                },
            ],
        }
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": ["INBOX"],
        "snippet": plain,
        "payload": payload,
    }


def test_gmail_search_messages_caches_token_and_parses_metadata() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "tok-1", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer tok-1"
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(
                200, json={"messages": [{"id": "m1", "threadId": "t1"}]}
            )
        if request.url.path == "/gmail/v1/users/me/messages/m1":
            headers = request.url.params.get_list("metadataHeaders")
            assert "From" in headers and "Subject" in headers
            return httpx.Response(200, json=_gmail_message())
        raise AssertionError(f"unexpected request {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GmailApiClient(
        "client-id",
        "client-secret",
        "refresh-token",
        client=http_client,
    )

    first = client.search_messages("from:alice", limit=5)
    second = client.search_messages("", limit=1)

    assert len(first) == 1 and len(second) == 1
    assert first[0]["subject"] == "Hello"
    assert first[0]["from"] == "Alice <alice@example.com>"
    assert first[0]["thread_id"] == "t1"
    token_calls = [c for c in calls if c.url.host == "oauth2.googleapis.com"]
    assert len(token_calls) == 1
    list_calls = [
        c
        for c in calls
        if c.url.path == "/gmail/v1/users/me/messages"
    ]
    assert list_calls[0].url.params["q"] == "from:alice"
    assert list_calls[0].url.params["labelIds"] == "INBOX"
    assert list_calls[1].url.params["maxResults"] == "1"


def test_gmail_refreshes_on_401_then_retries() -> None:
    state = {"token": 0, "messages": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{state['token']}",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/gmail/v1/users/me/messages":
            state["messages"] += 1
            if state["messages"] == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(
                200, json={"messages": [{"id": "m1", "threadId": "t1"}]}
            )
        if request.url.path == "/gmail/v1/users/me/messages/m1":
            return httpx.Response(200, json=_gmail_message())
        raise AssertionError(f"unexpected request {request.url}")

    client = GmailApiClient(
        "client-id",
        "client-secret",
        "refresh-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    messages = client.search_messages("", limit=1)

    assert len(messages) == 1
    assert state == {"token": 2, "messages": 2}


def test_gmail_read_thread_parses_nested_multipart() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        assert request.url.path == "/gmail/v1/users/me/threads/t1"
        assert request.url.params["format"] == "full"
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "messages": [
                    _gmail_message(
                        plain="plain body", html="<p>html body</p>"
                    )
                ],
            },
        )

    client = GmailApiClient(
        "id",
        "secret",
        "refresh",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    thread = client.read_thread("t1")

    assert thread["thread_id"] == "t1"
    assert thread["message_count"] == 1
    message = thread["messages"][0]
    assert message["body"] == "plain body"
    assert message["body_html"] == "<p>html body</p>"
    assert message["message_id"] == "<m1@example.com>"


def test_gmail_draft_and_send_build_base64url_rfc822() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        if request.url.path == "/gmail/v1/users/me/drafts":
            import json as json_module

            captured.append(json_module.loads(request.content))
            return httpx.Response(
                200,
                json={"id": "d1", "message": {"id": "m2", "threadId": "t2"}},
            )
        if request.url.path == "/gmail/v1/users/me/messages/send":
            import json as json_module

            captured.append(json_module.loads(request.content))
            return httpx.Response(200, json={"id": "m3", "threadId": "t3"})
        raise AssertionError(f"unexpected request {request.url}")

    client = GmailApiClient(
        "id",
        "secret",
        "refresh",
        from_address="me@example.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    draft = client.save_draft(
        to=["bob@example.com"],
        subject="Draft subject",
        body="draft body",
        cc="carol@example.com",
        reply_to_message_id="orig@example.com",
    )
    assert draft["id"] == "d1"
    assert draft["status"] == "draft"

    sent = client.send_message(
        "bob@example.com",
        "Reply",
        "reply body",
        reply_to_message_id="<orig@example.com>",
        idempotency_key="idem-9",
    )
    assert sent["status"] == "sent"
    assert sent["id"] == "m3"

    def decode(raw: str) -> Any:
        padded = raw + "=" * (-len(raw) % 4)
        return BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(padded)
        )

    draft_message = decode(captured[0]["message"]["raw"])
    assert draft_message["To"] == "bob@example.com"
    assert draft_message["Cc"] == "carol@example.com"
    assert draft_message["In-Reply-To"] == "<orig@example.com>"
    assert draft_message["References"] == "<orig@example.com>"
    sent_message = decode(captured[1]["raw"])
    assert sent_message["X-SmartCS-Idempotency-Key"] == "idem-9"
    assert sent_message["In-Reply-To"] == "<orig@example.com>"


def test_gmail_post_never_retries_on_transient_error() -> None:
    state = {"sends": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        state["sends"] += 1
        return httpx.Response(503, text="try later")

    client = GmailApiClient(
        "id",
        "secret",
        "refresh",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ConnectorError) as excinfo:
        client.send_message("bob@example.com", "subject", "body")
    assert state["sends"] == 1
    assert excinfo.value.status_code == 503


def test_gmail_token_failure_redacts_oauth_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text="bad request client-secret-value refresh-token-value",
        )

    client = GmailApiClient(
        "id",
        "client-secret-value",
        "refresh-token-value",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ConnectorError) as excinfo:
        client.search_messages("x")
    message = str(excinfo.value)
    assert "client-secret-value" not in message
    assert "refresh-token-value" not in message
    assert "HTTP 400" in message


def test_gmail_expired_cached_access_token_is_refreshed() -> None:
    state = {"tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            state["tokens"] += 1
            return httpx.Response(
                200, json={"access_token": "fresh-token", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer fresh-token"
        return httpx.Response(
            200, json={"messages": [], "resultSizeEstimate": 0}
        )

    client = GmailApiClient(
        "id",
        "secret",
        "refresh",
        access_token="stale-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client._token_expires_at = 0.0
    assert client.search_messages("", limit=1) == []
    assert state["tokens"] == 1


# --------------------------------------------------------------- router tests


def test_mail_router_selects_case_insensitively_and_rejects_missing() -> None:
    fake_imap = FakeIMAP({"INBOX": {}})
    provider = ImapSmtpMailClient(
        "me@163.com", "code", imap_factory=lambda: fake_imap
    )
    router = MailRouter({"163": provider, "Work": provider})

    assert router.get_provider("163") is provider
    assert router.get_provider("WORK") is provider
    assert router.accounts == ["163", "work"]
    with pytest.raises(ConnectorConfigError, match="No mail provider"):
        router.get_provider("gmail")


def test_mail_router_from_env_builds_configured_accounts() -> None:
    env = {
        "MAIL_163_USERNAME": "me@163.com",
        "MAIL_163_AUTH_CODE": "auth-code",
        "MAIL_163_IMAP_HOST": "imap.example.com",
        "MAIL_163_IMAP_PORT": "1993",
        "MAIL_163_SMTP_PORT": "1465",
        "MAIL_163_DRAFTS_FOLDER": "Drafts/Personal",
        "GMAIL_CLIENT_ID": "gid",
        "GMAIL_CLIENT_SECRET": "gsecret",
        "GMAIL_REFRESH_TOKEN": "grefresh",
    }
    router = MailRouter.from_env(env)

    assert router.accounts == ["163", "gmail"]
    imap_provider = router.get_provider("163")
    assert isinstance(imap_provider, ImapSmtpMailClient)
    assert imap_provider.imap_host == "imap.example.com"
    assert imap_provider.imap_port == 1993
    assert imap_provider.smtp_port == 1465
    assert imap_provider.drafts_folder == "Drafts/Personal"
    assert "auth-code" not in repr(imap_provider)


def test_mail_router_from_env_rejects_partial_configuration() -> None:
    with pytest.raises(ConnectorConfigError, match="MAIL_163_USERNAME"):
        MailRouter.from_env({"MAIL_163_USERNAME": "me@163.com"})
    with pytest.raises(ConnectorConfigError, match="GMAIL_CLIENT_ID"):
        MailRouter.from_env({"GMAIL_CLIENT_ID": "only-id"})
    with pytest.raises(ConnectorConfigError, match="MAIL_163_IMAP_PORT"):
        MailRouter.from_env(
            {
                "MAIL_163_USERNAME": "me@163.com",
                "MAIL_163_PASSWORD": "pw",
                "MAIL_163_IMAP_PORT": "not-a-port",
            }
        )


def test_mail_router_from_env_empty_when_nothing_configured() -> None:
    router = MailRouter.from_env({})
    assert router.accounts == []
    with pytest.raises(ConnectorConfigError):
        router.get_provider("163")


def test_canonical_payload_hash_is_order_independent() -> None:
    first = canonical_payload_hash({"a": 1, "b": {"x": "y"}})
    second = canonical_payload_hash({"b": {"x": "y"}, "a": 1})
    assert first == second
    assert first != canonical_payload_hash({"a": 2, "b": {"x": "y"}})
    assert len(first) == 64
