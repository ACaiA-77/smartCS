"""Feishu (Lark) OpenAPI connector with cached tenant access tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .base import ConnectorError, http_request_json

_DEFAULT_BASE_URL = "https://open.feishu.cn/open-apis"
_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
_TOKEN_INVALID_CODES = frozenset({99991661, 99991663, 99991668})
_MAX_UUID_LENGTH = 50
_MAX_PAGE_SIZE = 100


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip()
    if any(char in text for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _require_page_size(page_size: Any) -> int:
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size must be an integer")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
    return page_size


def verify_feishu_signature(
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    raw_body: bytes,
    signature: str,
) -> bool:
    """Verify a Feishu event/request signature.

    Feishu signs as ``SHA256(timestamp + nonce + encrypt_key + body)`` and
    sends the lowercase hex digest in ``X-Lark-Signature``.
    """

    if not signature:
        return False
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
    expected = hashlib.sha256(
        str(timestamp).encode("utf-8")
        + str(nonce).encode("utf-8")
        + str(encrypt_key).encode("utf-8")
        + body
    ).hexdigest()
    return hmac.compare_digest(expected, str(signature).strip().casefold())


class FeishuClient:
    """Feishu OpenAPI client with an automatically refreshed tenant token.

    The tenant token is cached until shortly before ``expire``; a token-invalid
    API code also triggers one refresh. Message writes never retry transient
    failures and accept ``uuid_key`` for Feishu-side idempotency.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not app_id or not str(app_id).strip():
            raise ValueError("app_id must not be empty")
        if not app_secret:
            raise ValueError("app_secret must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._app_id = str(app_id).strip()
        self._app_secret = str(app_secret)
        self._base_url = str(base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._tenant_token: str | None = None
        self._token_expires_at = 0.0

    def __repr__(self) -> str:
        return f"FeishuClient(base_url={self._base_url!r})"

    def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FeishuClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _api_error(data: Mapping[str, Any], context: str) -> ConnectorError:
        code = data.get("code")
        message = data.get("msg") or data.get("message") or ""
        error = ConnectorError(
            f"Feishu API error ({context}) code={code}: {message}"
        )
        error.code = code  # type: ignore[attr-defined]
        error.msg = message  # type: ignore[attr-defined]
        return error

    def _invalidate_token(self) -> None:
        self._tenant_token = None
        self._token_expires_at = 0.0

    def _fetch_token(self) -> str:
        data = http_request_json(
            self._client,
            "POST",
            f"{self._base_url}{_TOKEN_PATH}",
            retries=0,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        if not isinstance(data, Mapping):
            raise ConnectorError("Feishu token response was not a JSON object")
        if data.get("code") != 0:
            raise self._api_error(data, "tenant_access_token")
        token = data.get("tenant_access_token")
        if not token:
            raise ConnectorError(
                "Feishu token response did not include tenant_access_token"
            )
        try:
            expire = float(data.get("expire", 7200))
        except (TypeError, ValueError):
            expire = 7200.0
        self._tenant_token = str(token)
        self._token_expires_at = time.monotonic() + max(30.0, expire - 60.0)
        return self._tenant_token

    def _ensure_token(self) -> str:
        if self._tenant_token and time.monotonic() < self._token_expires_at:
            return self._tenant_token
        return self._fetch_token()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        read_retries: int = 2,
    ) -> dict[str, Any]:
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
                data = http_request_json(
                    self._client,
                    method,
                    url,
                    retries=read_retries if method in {"GET", "HEAD"} else 0,
                    **kwargs,
                )
            except ConnectorError as exc:
                if exc.status_code == 401 and attempt == 0:
                    self._invalidate_token()
                    continue
                raise

            if not isinstance(data, Mapping):
                raise ConnectorError("Feishu API returned a non-object response")
            if data.get("code") == 0:
                return dict(data)
            if data.get("code") in _TOKEN_INVALID_CODES and attempt == 0:
                self._invalidate_token()
                continue
            raise self._api_error(data, f"{method} {path}")
        raise ConnectorError("Feishu request failed after token refresh")

    def list_chats(self, page_size: int = 20) -> list[dict[str, Any]]:
        """List chats visible to the tenant application."""

        data = self._request(
            "GET",
            "/im/v1/chats",
            params={"page_size": _require_page_size(page_size)},
        )
        payload = data.get("data") or {}
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        return [item for item in items if isinstance(item, dict)]

    def read_messages(
        self,
        container_id: str,
        container_id_type: str = "chat",
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Read messages from a chat or thread container."""

        container_id = _require_text("container_id", container_id)
        container_id_type = _require_text("container_id_type", container_id_type)
        data = self._request(
            "GET",
            "/im/v1/messages",
            params={
                "container_id": container_id,
                "container_id_type": container_id_type,
                "page_size": _require_page_size(page_size),
            },
        )
        payload = data.get("data") or {}
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        return [item for item in items if isinstance(item, dict)]

    def read_doc(self, document_id: str) -> dict[str, Any]:
        """Read the plain-text content of a Feishu Docx document."""

        document_id = _require_text("document_id", document_id)
        data = self._request(
            "GET",
            f"/docx/v1/documents/{quote(document_id, safe='')}/raw_content",
        )
        payload = data.get("data") or {}
        result = dict(payload) if isinstance(payload, Mapping) else {}
        result.setdefault("document_id", document_id)
        return result

    def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str | Mapping[str, Any],
        *,
        uuid_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a message; ``uuid_key`` maps to Feishu's idempotency ``uuid``."""

        receive_id = _require_text("receive_id", receive_id)
        receive_id_type = _require_text("receive_id_type", receive_id_type)
        msg_type = _require_text("msg_type", msg_type)
        if isinstance(content, Mapping):
            content_text = json.dumps(
                content, ensure_ascii=False, separators=(",", ":")
            )
        elif isinstance(content, str):
            content_text = content
        else:
            raise ValueError("content must be a string or mapping")
        if not content_text:
            raise ValueError("content must not be empty")

        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content_text,
        }
        if uuid_key is not None:
            key = _require_text("uuid_key", uuid_key)
            if len(key) > _MAX_UUID_LENGTH:
                raise ValueError(
                    f"uuid_key must be at most {_MAX_UUID_LENGTH} characters"
                )
            payload["uuid"] = key

        data = self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body=payload,
        )
        result = data.get("data") or {}
        return dict(result) if isinstance(result, Mapping) else {}
