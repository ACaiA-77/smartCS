"""GitHub REST connector for notifications, issues, pulls and code search."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .base import http_request_json

_DEFAULT_BASE_URL = "https://api.github.com"
_MAX_PER_PAGE = 100


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip()
    if "/" in text:
        raise ValueError(f"{name} must not contain '/'")
    if any(char in text for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _require_number(number: Any) -> int:
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("number must be a positive integer")
    return number


def _require_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return min(limit, _MAX_PER_PAGE)


class GitHubClient:
    """Minimal GitHub REST client using a fine-grained personal access token.

    Reads may retry transient failures; write methods (issue creation and
    comments) never retry so the execution ledger stays the single source of
    idempotency. The token is sent only in the Authorization header and is
    redacted from all connector error messages.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token or not str(token).strip():
            raise ValueError("token must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._token = str(token).strip()
        self._base_url = str(base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"GitHubClient(base_url={self._base_url!r})"

    def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "smartcs-connectors",
        }

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
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        return http_request_json(
            self._client,
            method,
            f"{self._base_url}{path}",
            retries=read_retries if method in {"GET", "HEAD"} else 0,
            **kwargs,
        )

    def list_notifications(
        self,
        all_notifications: bool = False,
        participating: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List the authenticated user's GitHub notifications."""

        params: dict[str, Any] = {"per_page": _require_limit(limit)}
        if all_notifications:
            params["all"] = "true"
        if participating:
            params["participating"] = "true"
        data = self._request("GET", "/notifications", params=params)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def get_issue_or_pr(
        self, owner: str, repo: str, number: int
    ) -> dict[str, Any]:
        """Fetch an issue or pull request by number."""

        owner = _require_text("owner", owner)
        repo = _require_text("repo", repo)
        number = _require_number(number)
        data = self._request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/issues/{number}",
        )
        if not isinstance(data, dict):
            raise ValueError("GitHub returned an unexpected issue payload")
        return data

    def search_code(
        self, query: str, repo: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search code, optionally scoped to one ``owner/repo``."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        search_query = query.strip()
        if repo is not None:
            if not isinstance(repo, str) or repo.count("/") != 1:
                raise ValueError("repo must use the 'owner/name' format")
            owner, name = repo.split("/", 1)
            _require_text("owner", owner)
            _require_text("name", name)
            search_query = f"{search_query} repo:{owner}/{name}"
        params: dict[str, Any] = {
            "q": search_query,
            "per_page": _require_limit(limit),
        }
        data = self._request("GET", "/search/code", params=params)
        items = data.get("items", []) if isinstance(data, Mapping) else []
        return [item for item in items if isinstance(item, dict)]

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue. This write is never retried automatically."""

        owner = _require_text("owner", owner)
        repo = _require_text("repo", repo)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        if body is None or not isinstance(body, str):
            raise ValueError("body must be a string")
        payload: dict[str, Any] = {"title": title.strip(), "body": body}
        if labels:
            payload["labels"] = [str(label) for label in labels]
        data = self._request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
            json_body=payload,
        )
        if not isinstance(data, dict):
            raise ValueError("GitHub returned an unexpected issue payload")
        return data

    def post_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """Post an issue or pull-request comment. This write never retries."""

        owner = _require_text("owner", owner)
        repo = _require_text("repo", repo)
        number = _require_number(number)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body must be a non-empty string")
        data = self._request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/issues/{number}/comments",
            json_body={"body": body},
        )
        if not isinstance(data, dict):
            raise ValueError("GitHub returned an unexpected comment payload")
        return data
