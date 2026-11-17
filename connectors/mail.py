"""Unified mail provider abstraction for 163 IMAP/SMTP and Gmail REST."""

from __future__ import annotations

import base64
import imaplib
import os
import re
import smtplib
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any
from urllib.parse import quote

import httpx

from .base import (
    ConnectorConfigError,
    ConnectorError,
    canonical_payload_hash,
    http_request_json,
)

_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DEFAULT_GMAIL_BASE_URL = "https://gmail.googleapis.com"
_APPENDUID_RE = re.compile(rb"APPENDUID\s+\d+\s+(\d+)", re.IGNORECASE)
_TOKEN_INVALID_STATUS = 401


def _decode_header_value(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        parts = decode_header(text)
    except (LookupError, ValueError):
        return text.strip()
    chunks: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            encoding = charset or "utf-8"
            try:
                chunks.append(part.decode(encoding, errors="replace"))
            except LookupError:
                chunks.append(part.decode("utf-8", errors="replace"))
        else:
            chunks.append(part)
    return "".join(chunks).strip()


def _decode_address_header(values: list[str]) -> list[str]:
    decoded = [_decode_header_value(value) for value in values]
    addresses: list[str] = []
    for name, address in getaddresses(decoded):
        if address and name:
            addresses.append(f"{name} <{address}>")
        elif address:
            addresses.append(address)
        elif name:
            addresses.append(name)
    return addresses


def _normalize_addresses(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, str) else [str(item) for item in value]
    addresses: list[str] = []
    for raw in raw_values:
        raw = str(raw).strip()
        if not raw:
            continue
        parsed = getaddresses([raw])
        if not parsed:
            addresses.append(raw)
            continue
        for name, address in parsed:
            if address and name:
                addresses.append(f"{name} <{address}>")
            elif address:
                addresses.append(address)
            elif name:
                addresses.append(name)
    return addresses


def _normalize_message_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith("<") and "@" in text:
        return f"<{text}>"
    return text


def _reject_header_injection(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must not contain CR or LF characters")


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        content = part.get_payload()
        return str(content) if isinstance(content, str) else ""
    charsets: list[str] = []
    part_charset = part.get_content_charset()
    for candidate in (part_charset, "utf-8", "gb18030", "latin-1"):
        if candidate and candidate not in charsets:
            charsets.append(candidate)
    for charset in charsets:
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _extract_bodies(message: Message) -> tuple[str, str | None]:
    if not message.is_multipart():
        content_type = message.get_content_type()
        text = _part_text(message)
        return ("", text) if content_type == "text/html" else (text, None)

    plain: str | None = None
    html: str | None = None
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and plain is None:
            plain = _part_text(part)
        elif content_type == "text/html" and html is None:
            html = _part_text(part)
    return plain or "", html or None


def _split_references(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in str(value).split() if item]


def _parse_message(raw: bytes) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body, body_html = _extract_bodies(message)
    message_id = _decode_header_value(message.get("Message-ID"))
    return {
        "id": message_id,
        "message_id": message_id,
        "thread_id": message_id,
        "from": _decode_header_value(message.get("From")),
        "to": _decode_address_header(list(message.get_all("To", []))),
        "cc": _decode_address_header(list(message.get_all("Cc", []))),
        "subject": _decode_header_value(message.get("Subject")),
        "date": _decode_header_value(message.get("Date")),
        "in_reply_to": _decode_header_value(message.get("In-Reply-To")) or None,
        "references": _split_references(message.get("References")),
        "body": body,
        "body_html": body_html,
    }


def _parse_headers(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        return {
            "from": "",
            "to": [],
            "cc": [],
            "subject": "",
            "date": "",
            "message_id": "",
            "in_reply_to": None,
            "references": [],
        }
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {
        "from": _decode_header_value(message.get("From")),
        "to": _decode_address_header(list(message.get_all("To", []))),
        "cc": _decode_address_header(list(message.get_all("Cc", []))),
        "subject": _decode_header_value(message.get("Subject")),
        "date": _decode_header_value(message.get("Date")),
        "message_id": _decode_header_value(message.get("Message-ID")),
        "in_reply_to": _decode_header_value(message.get("In-Reply-To")) or None,
        "references": _split_references(message.get("References")),
    }


def _response_payload(response: Any) -> bytes:
    """Extract literal bytes from an ``imaplib`` FETCH response.

    Literals are carried in ``(prefix, literal)`` tuples; bare bytes in the
    same list are IMAP response markers such as ``b')'`` and must not become
    part of the parsed message.
    """

    if not isinstance(response, (tuple, list)) or len(response) < 2:
        return b""
    data = response[1]
    literal_chunks: list[bytes] = []
    bare_chunks: list[bytes] = []
    if isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, tuple):
                for part in item[1:]:
                    if isinstance(part, (bytes, bytearray)):
                        literal_chunks.append(bytes(part))
            elif isinstance(item, (bytes, bytearray)):
                bare_chunks.append(bytes(item))
    elif isinstance(data, (bytes, bytearray)):
        bare_chunks.append(bytes(data))
    return b"".join(literal_chunks if literal_chunks else bare_chunks)


def _search_uids(response: Any) -> list[str]:
    if not isinstance(response, (tuple, list)) or len(response) < 2:
        return []
    status, data = response[0], response[1]
    if str(status).upper() != "OK":
        return []
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, (list, tuple)):
        raw = b" ".join(
            bytes(item) for item in data if isinstance(item, (bytes, bytearray))
        )
    else:
        raw = b""
    return [token.decode("ascii", "ignore") for token in raw.split() if token.isdigit()]


def _imap_quote(value: str) -> str:
    _reject_header_injection("IMAP value", value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _message_payload(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    reply_to_message_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    return {
        "to": to,
        "cc": cc,
        "subject": subject,
        "body": body,
        "reply_to_message_id": reply_to_message_id,
        "idempotency_key": idempotency_key,
    }


def _build_email_message(
    *,
    to: list[str] | str | None,
    subject: str,
    body: str,
    cc: list[str] | str | None = None,
    reply_to_message_id: str | None = None,
    idempotency_key: str | None = None,
    from_address: str | None = None,
) -> tuple[EmailMessage, list[str], list[str]]:
    if not isinstance(subject, str):
        raise ValueError("subject must be a string")
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    _reject_header_injection("subject", subject)

    to_list = _normalize_addresses(to)
    cc_list = _normalize_addresses(cc)
    if not to_list:
        raise ValueError("at least one recipient is required")

    message = EmailMessage()
    if from_address:
        _reject_header_injection("from address", from_address)
        message["From"] = from_address
    message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    domain = from_address.rsplit("@", 1)[-1] if from_address and "@" in from_address else None
    message["Message-ID"] = make_msgid(domain=domain)

    normalized_reply = _normalize_message_id(reply_to_message_id)
    if normalized_reply:
        _reject_header_injection("reply_to_message_id", normalized_reply)
        message["In-Reply-To"] = normalized_reply
        message["References"] = normalized_reply

    if idempotency_key is not None:
        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key must not be empty")
        _reject_header_injection("idempotency_key", key)
        message["X-SmartCS-Idempotency-Key"] = key

    message.set_content(body)
    return message, to_list, cc_list


class MailProvider(ABC):
    """Common interface implemented by every mail backend."""

    @abstractmethod
    def search_messages(
        self, query: str, folder: str = "INBOX", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search a mailbox and return message summaries."""

    @abstractmethod
    def read_thread(self, thread_id: str) -> dict[str, Any]:
        """Return the messages that belong to a conversation."""

    @abstractmethod
    def save_draft(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a draft without sending it."""

    @abstractmethod
    def send_message(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a message, optionally carrying an idempotency marker."""


class ImapSmtpMailClient(MailProvider):
    """Mail access over IMAP for reading and SMTP for sending.

    The defaults target a 163 mailbox with SSL on ports 993/465 and an account
    authorization code. Factories can be injected to run fully offline.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        imap_host: str = "imap.163.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.163.com",
        smtp_port: int = 465,
        use_ssl: bool = True,
        drafts_folder: str = "Drafts",
        timeout: float = 15.0,
        from_address: str | None = None,
        imap_factory: Callable[[], Any] | None = None,
        smtp_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not username or not str(username).strip():
            raise ValueError("username must not be empty")
        if not password:
            raise ValueError("password must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.username = str(username).strip()
        self._password = str(password)
        self.imap_host = imap_host
        self.imap_port = int(imap_port)
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.use_ssl = bool(use_ssl)
        self.drafts_folder = drafts_folder or "Drafts"
        self.timeout = float(timeout)
        self.from_address = from_address or self.username
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp

    def __repr__(self) -> str:
        return (
            f"ImapSmtpMailClient(username={self.username!r}, "
            f"imap_host={self.imap_host!r}, smtp_host={self.smtp_host!r})"
        )

    def _default_imap(self) -> Any:
        if self.use_ssl:
            return imaplib.IMAP4_SSL(
                self.imap_host, self.imap_port, timeout=self.timeout
            )
        return imaplib.IMAP4(self.imap_host, self.imap_port, timeout=self.timeout)

    def _default_smtp(self) -> Any:
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout)
        return smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)

    def _open_imap(self) -> Any:
        imap = self._imap_factory()
        try:
            imap.login(self.username, self._password)
            self._maybe_send_id(imap)
            return imap
        except Exception as exc:
            self._close_imap(imap)
            raise ConnectorError(
                f"IMAP login failed for {self.username} at {self.imap_host}: "
                f"{type(exc).__name__}"
            ) from None

    @staticmethod
    def _close_imap(imap: Any) -> None:
        try:
            imap.logout()
        except Exception:
            pass

    @staticmethod
    def _maybe_send_id(imap: Any) -> None:
        """163 rejects clients that do not send the IMAP ID command."""

        xatom = getattr(imap, "xatom", None)
        if xatom is None:
            return
        try:
            imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
            xatom(
                "ID",
                '("name" "SmartCS" "version" "1.0" "vendor" "smartcs")',
            )
        except Exception:
            # The ID command is a compatibility nicety, never a hard failure.
            pass

    def _select(self, imap: Any, folder: str, *, readonly: bool) -> None:
        if not folder or not str(folder).strip():
            raise ValueError("folder must not be empty")
        _reject_header_injection("folder", str(folder))
        status, _ = imap.select(_imap_quote(str(folder)), readonly=readonly)
        if str(status).upper() != "OK":
            raise ConnectorError(f"Unable to select IMAP folder {folder!r}")

    def _fetch_message(self, imap: Any, uid: str, item: str) -> bytes:
        status, data = imap.uid("FETCH", uid, item)
        if str(status).upper() != "OK":
            raise ConnectorError(f"Unable to fetch message {uid!r}")
        return _response_payload((status, data))

    def search_messages(
        self, query: str, folder: str = "INBOX", limit: int = 10
    ) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        text = str(query or "").strip()
        criteria: tuple[str, ...]
        if not text or text == "*":
            criteria = ("ALL",)
        else:
            criteria = ("TEXT", _imap_quote(text))

        imap = self._open_imap()
        try:
            self._select(imap, folder, readonly=True)
            status, data = imap.uid("SEARCH", None, *criteria)
            uids = _search_uids((status, data))
            selected = list(reversed(uids[-limit:]))
            summaries: list[dict[str, Any]] = []
            for uid in selected:
                raw = self._fetch_message(
                    imap,
                    uid,
                    "(BODY.PEEK[HEADER.FIELDS "
                    "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])",
                )
                summary = _parse_headers(raw)
                summary.update({"id": uid, "uid": uid, "thread_id": uid})
                summaries.append(summary)
            return summaries
        finally:
            self._close_imap(imap)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        text = str(thread_id or "").strip()
        if not text:
            raise ValueError("thread_id must not be empty")
        imap = self._open_imap()
        try:
            self._select(imap, "INBOX", readonly=True)
            if text.isdigit():
                uid = text
            else:
                status, data = imap.uid(
                    "SEARCH", None, "HEADER", "Message-ID", _imap_quote(text)
                )
                matches = _search_uids((status, data))
                if not matches:
                    raise ConnectorError(f"Message {text!r} was not found")
                uid = matches[-1]
            raw = self._fetch_message(imap, uid, "(RFC822)")
            if not raw.strip():
                raise ConnectorError(f"Message {text!r} returned no content")
            parsed = _parse_message(raw)
            parsed.update({"uid": uid, "thread_id": text, "folder": "INBOX"})
            return {
                "thread_id": text,
                "folder": "INBOX",
                "subject": parsed["subject"],
                "message_count": 1,
                "messages": [parsed],
            }
        finally:
            self._close_imap(imap)

    def save_draft(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        message, to_list, cc_list = _build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            reply_to_message_id=reply_to_message_id,
            from_address=self.from_address,
        )
        payload_hash = canonical_payload_hash(
            _message_payload(
                to=to_list,
                cc=cc_list,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
                idempotency_key=None,
            )
        )
        imap = self._open_imap()
        try:
            status, data = imap.append(
                _imap_quote(self.drafts_folder),
                r"(\Draft)",
                imaplib.Time2Internaldate(time.time()),
                message.as_bytes(),
            )
            if str(status).upper() != "OK":
                raise ConnectorError(
                    f"Unable to append draft to {self.drafts_folder!r}"
                )
            uid: str | None = None
            for item in data or []:
                if isinstance(item, (bytes, bytearray)):
                    match = _APPENDUID_RE.search(bytes(item))
                    if match:
                        uid = match.group(1).decode("ascii", "ignore")
                        break
            return {
                "id": uid or message["Message-ID"],
                "uid": uid,
                "folder": self.drafts_folder,
                "status": "draft",
                "message_id": str(message["Message-ID"]),
                "to": to_list,
                "cc": cc_list,
                "subject": subject,
                "payload_hash": payload_hash,
            }
        finally:
            self._close_imap(imap)

    def send_message(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        message, to_list, cc_list = _build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            reply_to_message_id=reply_to_message_id,
            idempotency_key=idempotency_key,
            from_address=self.from_address,
        )
        payload_hash = canonical_payload_hash(
            _message_payload(
                to=to_list,
                cc=cc_list,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
                idempotency_key=idempotency_key,
            )
        )
        smtp = self._smtp_factory()
        try:
            try:
                smtp.login(self.username, self._password)
                refused = smtp.send_message(message)
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(
                    f"SMTP send failed via {self.smtp_host}: "
                    f"{type(exc).__name__}"
                ) from None
            if refused:
                recipients = ", ".join(sorted(refused))
                raise ConnectorError(
                    f"SMTP refused recipients: {recipients}"
                )
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return {
            "status": "sent",
            "message_id": str(message["Message-ID"]),
            "to": to_list,
            "cc": cc_list,
            "subject": subject,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
        }


def _decode_b64url(value: str) -> str:
    data = str(value)
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _header_map(payload: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if not isinstance(header, Mapping):
            continue
        name = str(header.get("name", "")).casefold()
        value = str(header.get("value", ""))
        if name:
            headers[name] = value
    return headers


def _extract_gmail_bodies(payload: Mapping[str, Any] | None) -> tuple[str, str | None]:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: Mapping[str, Any]) -> None:
        mime_type = str(part.get("mimeType", ""))
        body = part.get("body") or {}
        data = body.get("data") if isinstance(body, Mapping) else None
        if isinstance(data, str) and data:
            if mime_type == "text/plain":
                plain.append(_decode_b64url(data))
            elif mime_type == "text/html":
                html.append(_decode_b64url(data))
        for child in part.get("parts") or []:
            if isinstance(child, Mapping):
                walk(child)

    if isinstance(payload, Mapping):
        walk(payload)
    return "\n".join(item for item in plain if item), (
        "\n".join(item for item in html if item) or None
    )


def _parse_gmail_message(message: Mapping[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _header_map(payload if isinstance(payload, Mapping) else {})
    body, body_html = _extract_gmail_bodies(
        payload if isinstance(payload, Mapping) else None
    )
    message_id = headers.get("message-id", "")
    return {
        "id": str(message.get("id", "")),
        "message_id": message_id,
        "thread_id": str(message.get("threadId", "")),
        "from": headers.get("from", ""),
        "to": _decode_address_header([headers["to"]]) if headers.get("to") else [],
        "cc": _decode_address_header([headers["cc"]]) if headers.get("cc") else [],
        "subject": _decode_header_value(headers.get("subject", "")),
        "date": headers.get("date", ""),
        "in_reply_to": headers.get("in-reply-to", "") or None,
        "references": _split_references(headers.get("references")),
        "body": body,
        "body_html": body_html,
        "snippet": str(message.get("snippet", "")),
        "label_ids": list(message.get("labelIds") or []),
    }


class GmailApiClient(MailProvider):
    """Gmail REST access with a cached OAuth2 refresh-token access token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        token_uri: str = _DEFAULT_TOKEN_URI,
        base_url: str = _DEFAULT_GMAIL_BASE_URL,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
        access_token: str | None = None,
        user_id: str = "me",
        from_address: str | None = None,
    ) -> None:
        if not client_id or not str(client_id).strip():
            raise ValueError("client_id must not be empty")
        if not client_secret:
            raise ValueError("client_secret must not be empty")
        if not refresh_token:
            raise ValueError("refresh_token must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._client_id = str(client_id).strip()
        self._client_secret = str(client_secret)
        self._refresh_token = str(refresh_token)
        self._token_uri = token_uri
        self._base_url = str(base_url).rstrip("/")
        self._user_id = quote(str(user_id), safe="")
        self._from_address = from_address
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._access_token = str(access_token) if access_token else None
        self._token_expires_at = time.monotonic() + 3600 if access_token else 0.0

    def __repr__(self) -> str:
        return f"GmailApiClient(base_url={self._base_url!r})"

    def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GmailApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _invalidate_token(self) -> None:
        self._access_token = None
        self._token_expires_at = 0.0

    def _refresh_access_token(self) -> str:
        data = http_request_json(
            self._client,
            "POST",
            self._token_uri,
            retries=0,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        token = data.get("access_token")
        if not token:
            raise ConnectorError("Gmail token response did not include access_token")
        self._access_token = str(token)
        try:
            expires_in = float(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._token_expires_at = time.monotonic() + max(30.0, expires_in - 60.0)
        return self._access_token

    def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        return self._refresh_access_token()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        read_retries: int = 2,
    ) -> Any:
        method = method.upper()
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            token = self._ensure_token()
            kwargs: dict[str, Any] = {
                "headers": {"Authorization": f"Bearer {token}"},
            }
            if params is not None:
                kwargs["params"] = params
            if json_body is not None:
                kwargs["json"] = json_body
            try:
                return http_request_json(
                    self._client,
                    method,
                    url,
                    retries=read_retries if method in {"GET", "HEAD"} else 0,
                    **kwargs,
                )
            except ConnectorError as exc:
                if exc.status_code == _TOKEN_INVALID_STATUS and attempt == 0:
                    self._invalidate_token()
                    continue
                raise
        raise ConnectorError("Gmail request failed after token refresh")

    def search_messages(
        self, query: str, folder: str = "INBOX", limit: int = 10
    ) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        params: dict[str, Any] = {"maxResults": min(limit, 500)}
        text = str(query or "").strip()
        if text:
            params["q"] = text
        if folder and str(folder).upper() not in {"", "ALL", "*"}:
            params["labelIds"] = str(folder)
        data = self._request(
            "GET", f"/gmail/v1/users/{self._user_id}/messages", params=params
        )
        messages = data.get("messages", []) if isinstance(data, Mapping) else []
        summaries: list[dict[str, Any]] = []
        for item in messages[:limit]:
            message_id = str(item.get("id", ""))
            if not message_id:
                continue
            detail = self._request(
                "GET",
                f"/gmail/v1/users/{self._user_id}/messages/"
                f"{quote(message_id, safe='')}",
                params={
                    "format": "metadata",
                    "metadataHeaders": [
                        "From",
                        "To",
                        "Cc",
                        "Subject",
                        "Date",
                        "Message-ID",
                    ],
                },
            )
            parsed = _parse_gmail_message(
                detail if isinstance(detail, Mapping) else item
            )
            if not parsed["thread_id"]:
                parsed["thread_id"] = str(item.get("threadId", ""))
            summaries.append(parsed)
        return summaries

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        text = str(thread_id or "").strip()
        if not text:
            raise ValueError("thread_id must not be empty")
        data = self._request(
            "GET",
            f"/gmail/v1/users/{self._user_id}/threads/{quote(text, safe='')}",
            params={"format": "full"},
        )
        raw_messages = data.get("messages", []) if isinstance(data, Mapping) else []
        messages = [
            _parse_gmail_message(message)
            for message in raw_messages
            if isinstance(message, Mapping)
        ]
        subject = messages[0]["subject"] if messages else ""
        return {
            "thread_id": str(data.get("id", text)) if isinstance(data, Mapping) else text,
            "subject": subject,
            "message_count": len(messages),
            "messages": messages,
        }

    def save_draft(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        message, to_list, cc_list = _build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            reply_to_message_id=reply_to_message_id,
            from_address=self._from_address,
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        payload_hash = canonical_payload_hash(
            _message_payload(
                to=to_list,
                cc=cc_list,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
                idempotency_key=None,
            )
        )
        data = self._request(
            "POST",
            f"/gmail/v1/users/{self._user_id}/drafts",
            json_body={"message": {"raw": raw}},
        )
        draft = data if isinstance(data, Mapping) else {}
        nested = draft.get("message") if isinstance(draft.get("message"), Mapping) else {}
        return {
            "id": str(draft.get("id", "")),
            "message_id": str(nested.get("id", "")),
            "thread_id": str(nested.get("threadId", "")),
            "status": "draft",
            "to": to_list,
            "cc": cc_list,
            "subject": subject,
            "payload_hash": payload_hash,
        }

    def send_message(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: list[str] | str | None = None,
        reply_to_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        message, to_list, cc_list = _build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            reply_to_message_id=reply_to_message_id,
            idempotency_key=idempotency_key,
            from_address=self._from_address,
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        payload_hash = canonical_payload_hash(
            _message_payload(
                to=to_list,
                cc=cc_list,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
                idempotency_key=idempotency_key,
            )
        )
        data = self._request(
            "POST",
            f"/gmail/v1/users/{self._user_id}/messages/send",
            json_body={"raw": raw},
        )
        sent = data if isinstance(data, Mapping) else {}
        return {
            "status": "sent",
            "id": str(sent.get("id", "")),
            "message_id": str(sent.get("id", "")),
            "thread_id": str(sent.get("threadId", "")),
            "to": to_list,
            "cc": cc_list,
            "subject": subject,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
        }


class MailRouter:
    """Select a :class:`MailProvider` by account name."""

    def __init__(self, providers: Mapping[str, MailProvider]) -> None:
        self._providers = {
            str(account).strip().casefold(): provider
            for account, provider in providers.items()
        }

    @property
    def accounts(self) -> list[str]:
        """Return the configured account names in sorted order."""

        return sorted(self._providers)

    def get_provider(self, account: str) -> MailProvider:
        """Return the provider for ``account`` or raise a config error."""

        key = str(account or "").strip().casefold()
        if not key or key not in self._providers:
            raise ConnectorConfigError(
                f"No mail provider configured for account {account!r}"
            )
        return self._providers[key]

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> MailRouter:
        """Build a router from ``MAIL_163_*`` and ``GMAIL_*`` variables."""

        values = os.environ if env is None else env
        providers: dict[str, MailProvider] = {}

        username = _env_value(values, "MAIL_163_USERNAME")
        password = _env_value(values, "MAIL_163_PASSWORD") or _env_value(
            values, "MAIL_163_AUTH_CODE"
        )
        if username and password:
            providers["163"] = ImapSmtpMailClient(
                username,
                password,
                imap_host=_env_value(values, "MAIL_163_IMAP_HOST") or "imap.163.com",
                imap_port=_env_port(values, "MAIL_163_IMAP_PORT", 993),
                smtp_host=_env_value(values, "MAIL_163_SMTP_HOST") or "smtp.163.com",
                smtp_port=_env_port(values, "MAIL_163_SMTP_PORT", 465),
                drafts_folder=_env_value(values, "MAIL_163_DRAFTS_FOLDER")
                or "Drafts",
                from_address=_env_value(values, "MAIL_163_FROM_ADDRESS"),
            )
        elif bool(username) != bool(password):
            raise ConnectorConfigError(
                "MAIL_163_USERNAME and MAIL_163_PASSWORD/AUTH_CODE "
                "must be set together"
            )

        gmail_id = _env_value(values, "GMAIL_CLIENT_ID")
        gmail_secret = _env_value(values, "GMAIL_CLIENT_SECRET")
        gmail_refresh = _env_value(values, "GMAIL_REFRESH_TOKEN")
        if gmail_id and gmail_secret and gmail_refresh:
            providers["gmail"] = GmailApiClient(
                gmail_id,
                gmail_secret,
                gmail_refresh,
                token_uri=_env_value(values, "GMAIL_TOKEN_URI") or _DEFAULT_TOKEN_URI,
                base_url=_env_value(values, "GMAIL_BASE_URL")
                or _DEFAULT_GMAIL_BASE_URL,
                access_token=_env_value(values, "GMAIL_ACCESS_TOKEN"),
                from_address=_env_value(values, "GMAIL_FROM_ADDRESS"),
            )
        elif any((gmail_id, gmail_secret, gmail_refresh)):
            raise ConnectorConfigError(
                "GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN "
                "must be set together"
            )

        return cls(providers)


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _env_port(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _env_value(env, key)
    if raw is None:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise ConnectorConfigError(f"{key} must be an integer port") from None
    if not 1 <= port <= 65535:
        raise ConnectorConfigError(f"{key} must be between 1 and 65535")
    return port
