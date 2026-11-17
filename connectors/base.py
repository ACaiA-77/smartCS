"""Shared HTTP, retry and canonical hashing helpers for external connectors."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

_SAFE_METHODS = frozenset({"GET", "HEAD"})
_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "app_secret",
        "auth",
        "authorization",
        "client_secret",
        "code",
        "credential",
        "credentials",
        "encrypt_key",
        "key",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "signature",
        "state",
        "tenant_access_token",
        "token",
    }
)
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "client-secret", "cookie", "proxy-authorization", "x-api-key"}
)
_REDACTED = "***"
_ERROR_SNIPPET_LIMIT = 300


class ConnectorError(RuntimeError):
    """Raised when an external connector cannot complete an operation.

    ``status_code`` and ``response_body`` are populated for HTTP failures and
    are already sanitized so they are safe to log.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ConnectorConfigError(ConnectorError):
    """Raised when required connector configuration is missing or invalid."""


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 hash for a connector write payload.

    The JSON is serialized with sorted keys and no insignificant whitespace so
    equivalent payloads hash identically before being recorded in the
    execution ledger.
    """

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iter_secret_values(value: Any, key: str | None = None) -> Iterator[str]:
    if (
        key is not None
        and key.casefold() in _SENSITIVE_KEYS
        and isinstance(value, (str, bytes))
    ):
        secret = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if secret:
            yield secret
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            yield from _iter_secret_values(child_value, str(child_key))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _iter_secret_values(child)


def _collect_secrets(kwargs: Mapping[str, Any], url: str) -> list[str]:
    secrets: set[str] = set()
    headers = kwargs.get("headers")
    if isinstance(headers, Mapping):
        for header_name, header_value in headers.items():
            if str(header_name).casefold() not in _SENSITIVE_HEADERS:
                continue
            text = str(header_value)
            secrets.add(text)
            scheme, _, credential = text.partition(" ")
            if scheme.casefold() in {"bearer", "token", "basic"} and credential:
                secrets.add(credential)
    for source_name in ("json", "data", "params"):
        secrets.update(_iter_secret_values(kwargs.get(source_name)))
    for query_key, query_value in parse_qsl(urlsplit(str(url)).query, keep_blank_values=True):
        if query_key.casefold() in _SENSITIVE_KEYS and query_value:
            secrets.add(query_value)
    return [secret for secret in secrets if secret]


def _sanitize(text: str, secrets: list[str]) -> str:
    sanitized = str(text)
    for secret in sorted(secrets, key=len, reverse=True):
        sanitized = sanitized.replace(secret, _REDACTED)
    return sanitized


def _safe_url(url: str) -> str:
    parts = urlsplit(str(url))
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    redacted_pairs = [
        (key, _REDACTED if key.casefold() in _SENSITIVE_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(redacted_pairs), "")
    )


def _response_snippet(response: httpx.Response) -> str:
    try:
        text = response.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return "<unreadable response body>"
    return " ".join(text.split())[:_ERROR_SNIPPET_LIMIT]


def _backoff(attempt: int) -> None:
    # Tests and short-lived CLI calls must stay fast; the retry count is small.
    time.sleep(min(0.05 * attempt, 0.2))


def http_request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 1,
    retry_safe_only: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform an HTTP request and return decoded JSON.

    Only ``GET`` and ``HEAD`` requests are retried by default, and only for
    transport failures or HTTP 502/503/504. Non-idempotent writes therefore
    never retry unless a caller explicitly disables ``retry_safe_only``.
    HTTP and decoding failures raise :class:`ConnectorError` with secrets
    removed from the message.
    """

    method = str(method).upper()
    max_retries = max(0, int(retries))
    if retry_safe_only and method not in _SAFE_METHODS:
        max_retries = 0

    secrets = _collect_secrets(kwargs, url)
    safe_url = _safe_url(url)
    attempt = 0
    response: httpx.Response | None = None

    while True:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt < max_retries:
                attempt += 1
                _backoff(attempt)
                continue
            detail = _sanitize(str(exc), secrets)
            raise ConnectorError(
                f"{method} {safe_url} transport error "
                f"({type(exc).__name__}): {detail}"
            ) from None

        if (
            response.status_code in _RETRYABLE_STATUS_CODES
            and attempt < max_retries
        ):
            attempt += 1
            _backoff(attempt)
            continue
        break

    assert response is not None
    if response.status_code >= 400:
        snippet = _sanitize(_response_snippet(response), secrets)
        message = f"{method} {safe_url} returned HTTP {response.status_code}"
        if snippet:
            message = f"{message}: {snippet}"
        raise ConnectorError(
            message,
            status_code=response.status_code,
            response_body=snippet,
        )

    if not response.content:
        return {}

    try:
        data = response.json()
    except ValueError:
        snippet = _sanitize(_response_snippet(response), secrets)
        raise ConnectorError(
            f"{method} {safe_url} returned a non-JSON response: {snippet}",
            status_code=response.status_code,
            response_body=snippet,
        ) from None

    # GitHub list endpoints legitimately return a JSON array. The annotation is
    # the common case; callers that need an array narrow the result themselves.
    return data
