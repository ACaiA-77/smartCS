"""Offline acceptance tests for the GitHub REST connector."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from connectors import ConnectorError, GitHubClient
from connectors.base import canonical_payload_hash, http_request_json

TOKEN = "github_pat_super_secret_token"


def _client(handler: Any) -> GitHubClient:
    return GitHubClient(
        TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_list_notifications_sends_auth_and_filters() -> None:
    seen: list[httpx.Request] = []
    payload = [
        {"id": "1", "reason": "mention", "unread": True},
        {"id": "2", "reason": "review_requested", "unread": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert request.headers["accept"] == "application/vnd.github+json"
        return httpx.Response(200, json=payload)

    client = _client(handler)
    notifications = client.list_notifications(
        all_notifications=True, participating=True, limit=5
    )

    assert notifications == payload
    assert len(seen) == 1
    assert seen[0].url.path == "/notifications"
    assert seen[0].url.params["per_page"] == "5"
    assert seen[0].url.params["all"] == "true"
    assert seen[0].url.params["participating"] == "true"


def test_get_issue_or_pr_and_search_code() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/octocat/hello/issues/42":
            return httpx.Response(
                200, json={"number": 42, "title": "An issue", "state": "open"}
            )
        if request.url.path == "/search/code":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [{"name": "main.py", "path": "src/main.py"}],
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    client = _client(handler)

    issue = client.get_issue_or_pr("octocat", "hello", 42)
    assert issue["number"] == 42

    results = client.search_code("needle", repo="octocat/hello", limit=3)
    assert results == [{"name": "main.py", "path": "src/main.py"}]
    search = seen[-1]
    assert search.url.params["q"] == "needle repo:octocat/hello"
    assert search.url.params["per_page"] == "3"


def test_create_issue_and_post_comment_payloads() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/octocat/hello/issues":
            return httpx.Response(201, json={"number": 7, "title": "New"})
        if request.url.path == "/repos/octocat/hello/issues/7/comments":
            return httpx.Response(201, json={"id": 99, "body": "Looks good"})
        raise AssertionError(f"unexpected request {request.url}")

    client = _client(handler)

    created = client.create_issue(
        "octocat",
        "hello",
        "New",
        body="Issue body",
        labels=["bug", "triage"],
    )
    assert created["number"] == 7
    issue_request = json.loads(seen[0].content)
    assert issue_request == {
        "title": "New",
        "body": "Issue body",
        "labels": ["bug", "triage"],
    }

    comment = client.post_comment("octocat", "hello", 7, "Looks good")
    assert comment["id"] == 99
    assert json.loads(seen[1].content) == {"body": "Looks good"}


def test_create_issue_without_body_is_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"title": "Title only", "body": ""}
        return httpx.Response(201, json={"number": 1})

    client = _client(handler)
    assert client.create_issue("o", "r", "Title only")["number"] == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.list_notifications(limit=0),
        lambda client: client.get_issue_or_pr("", "repo", 1),
        lambda client: client.get_issue_or_pr("owner", "", 1),
        lambda client: client.get_issue_or_pr("owner", "repo", 0),
        lambda client: client.get_issue_or_pr("owner", "repo", True),
        lambda client: client.search_code(""),
        lambda client: client.search_code("q", repo="not-a-slug"),
        lambda client: client.create_issue("owner", "repo", ""),
        lambda client: client.post_comment("owner", "repo", 1, "  "),
        lambda client: client.post_comment("owner", "repo", 0, "body"),
    ],
)
def test_input_validation(operation: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("validation must happen before any request")

    client = _client(handler)
    with pytest.raises(ValueError):
        operation(client)


def test_create_issue_never_retries_writes_on_transient_error() -> None:
    state = {"writes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["writes"] += 1
        return httpx.Response(503, text="try later")

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.create_issue("owner", "repo", "Title")
    assert state["writes"] == 1
    assert excinfo.value.status_code == 503


def test_post_comment_never_retries_writes_on_transport_error() -> None:
    state = {"writes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["writes"] += 1
        raise httpx.ConnectError("connection reset")

    client = _client(handler)
    with pytest.raises(ConnectorError):
        client.post_comment("owner", "repo", 1, "body")
    assert state["writes"] == 1


def test_reads_retry_transient_status_then_succeed() -> None:
    state = {"reads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["reads"] += 1
        if state["reads"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"number": 1, "title": "ok"})

    client = _client(handler)
    assert client.get_issue_or_pr("owner", "repo", 1)["title"] == "ok"
    assert state["reads"] == 2


def test_reads_retry_transport_errors_then_succeed() -> None:
    state = {"reads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["reads"] += 1
        if state["reads"] == 1:
            raise httpx.ConnectError("temporary")
        return httpx.Response(200, json={"number": 1, "title": "ok"})

    client = _client(handler)
    assert client.get_issue_or_pr("owner", "repo", 1)["title"] == "ok"
    assert state["reads"] == 2


def test_reads_raise_after_retry_budget_is_exhausted() -> None:
    state = {"reads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["reads"] += 1
        return httpx.Response(502, text="bad gateway")

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.list_notifications()
    assert state["reads"] == 3
    assert excinfo.value.status_code == 502


def test_token_is_redacted_from_error_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=f"bad credentials for {TOKEN} in Authorization: Bearer {TOKEN}",
        )

    client = _client(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.get_issue_or_pr("owner", "repo", 1)
    message = str(excinfo.value)
    assert TOKEN not in message
    assert message.count("***") >= 1
    assert "HTTP 401" in message


def test_base_helper_never_retries_post_by_default() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(503, text="busy")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectorError):
        http_request_json(client, "POST", "https://example.test/write", retries=5)
    assert state["calls"] == 1


def test_base_helper_retries_only_safe_methods_by_default() -> None:
    state = {"GET": 0, "POST": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state[request.method] += 1
        if state[request.method] == 1:
            return httpx.Response(504, text="timeout")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert http_request_json(
        client, "GET", "https://example.test/read", retries=2
    ) == {"ok": True}
    assert state["GET"] == 2

    with pytest.raises(ConnectorError):
        http_request_json(
            client, "POST", "https://example.test/write", retries=2
        )
    assert state["POST"] == 1


def test_base_helper_retries_post_only_when_explicitly_opted_in() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert http_request_json(
        client,
        "POST",
        "https://example.test/write",
        retries=2,
        retry_safe_only=False,
    ) == {"ok": True}
    assert state["calls"] == 2


def test_canonical_hash_matches_shared_helper() -> None:
    payload = {"b": 2, "a": 1}
    assert canonical_payload_hash(payload) == canonical_payload_hash(
        {"a": 1, "b": 2}
    )


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValueError):
        GitHubClient("  ")
