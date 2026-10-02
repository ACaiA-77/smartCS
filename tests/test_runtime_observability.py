from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI

import api.main as api_main
from auth.context import UserContext
from mcp.execution_ledger import ExecutionLedger
from mcp.execution_recovery import ExecutionReconciler
from mcp.mcp_server import MCPToolServer, ToolDefinition
from mcp.tool_execution import ToolExecutionContext, ToolExecutionResult, ToolExecutor
from tracing import otel_config
from tracing.observability import (
    InstrumentedExecutionReconciler,
    InstrumentedToolExecutor,
    RuntimeMetrics,
    get_request_id,
    install_request_observability,
    is_safe_request_id,
    request_id_from_header,
)


async def _raw_asgi_request(app, path: str, headers: list[tuple[bytes, bytes]]):
    path_only, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path_only,
        "raw_path": path_only.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "headers": [(b"host", b"test"), *headers],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    messages = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


@pytest.mark.asyncio
async def test_tool_metrics_cover_outcomes_without_payload_logging(tmp_path, caplog):
    server = MCPToolServer()
    retry_calls = 0

    async def read_ok():
        return {"success": True, "value": "SECRET_USER_123"}

    async def retry_read():
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise RuntimeError("SECRET_DESCRIPTION_789")
        return {"success": True}

    async def timeout_read():
        await asyncio.sleep(0.02)
        return {"success": True}

    async def confirmed_write():
        return {"success": True, "order_id": "SECRET_ORDER_456"}

    async def rejected_read():
        return {"success": False, "reason": "SECRET_DESCRIPTION_789"}

    definitions = [
        ("read_ok", read_ok, "read", False, True),
        ("retry_read", retry_read, "read", False, True),
        ("timeout_read", timeout_read, "read", False, False),
        ("confirmed_write", confirmed_write, "write", False, True),
        ("rejected_read", rejected_read, "read", False, False),
        ("confirm_read", read_ok, "read", True, False),
    ]
    for name, handler, operation_type, requires_confirmation, retryable in definitions:
        server.register_tool(
            ToolDefinition(
                name=name,
                description=name,
                input_schema={"type": "object"},
                handler=handler,
                operation_type=operation_type,
                requires_confirmation=requires_confirmation,
                retryable=retryable,
            )
        )

    metrics = RuntimeMetrics()
    executor = InstrumentedToolExecutor(
        server,
        ledger=ExecutionLedger(tmp_path / "ledger.db"),
        timeout_seconds=0.001,
        runtime_metrics=metrics,
    )

    with caplog.at_level(logging.INFO, logger="tracing.observability"):
        successful_read = await executor.execute("read_ok", {})
        retried = await executor.execute("retry_read", {})
        timed_out = await executor.execute("timeout_read", {})
        confirmation = await executor.execute("confirm_read", {})
        write = await executor.execute(
            "confirmed_write",
            {},
            context=ToolExecutionContext(confirmed=True, idempotency_key="write-1"),
        )
        replay = await executor.execute(
            "confirmed_write",
            {},
            context=ToolExecutionContext(confirmed=True, idempotency_key="write-1"),
        )
        rejected = await executor.execute("rejected_read", {})

    assert successful_read.success is True
    assert retried.attempts == 2
    assert timed_out.error_code == "timeout"
    assert confirmation.status == "confirmation_required"
    assert write.success is True
    assert replay.replayed is True
    assert rejected.success is True

    tools = metrics.snapshot()["tools"]
    read_ok_metrics = {
        key: value for key, value in tools["read_ok"].items() if key != "avg_duration_ms"
    }
    assert read_ok_metrics == {
        "calls": 1,
        "transport_success": 1,
        "transport_failure": 0,
        "business_rejections": 0,
        "replays": 0,
        "timeouts": 0,
        "confirmation_required": 0,
        "attempts_total": 1,
        "retry_attempts": 0,
    }
    assert tools["retry_read"]["calls"] == 1
    assert tools["retry_read"]["transport_success"] == 1
    assert tools["retry_read"]["transport_failure"] == 0
    assert tools["retry_read"]["attempts_total"] == 2
    assert tools["retry_read"]["retry_attempts"] == 1
    assert tools["timeout_read"]["calls"] == 1
    assert tools["timeout_read"]["transport_success"] == 0
    assert tools["timeout_read"]["transport_failure"] == 1
    assert tools["timeout_read"]["timeouts"] == 1
    assert tools["confirm_read"]["calls"] == 1
    assert tools["confirm_read"]["transport_failure"] == 1
    assert tools["confirm_read"]["confirmation_required"] == 1
    assert tools["confirmed_write"]["calls"] == 2
    assert tools["confirmed_write"]["transport_success"] == 2
    assert tools["confirmed_write"]["attempts_total"] == 1
    assert tools["confirmed_write"]["replays"] == 1
    assert tools["rejected_read"]["calls"] == 1
    assert tools["rejected_read"]["transport_success"] == 1
    assert tools["rejected_read"]["transport_failure"] == 0
    assert tools["rejected_read"]["business_rejections"] == 1
    assert sum(item["calls"] for item in tools.values()) == 7
    assert sum(item["transport_failure"] for item in tools.values()) == 2
    assert "SECRET_USER_123" not in caplog.text
    assert "SECRET_ORDER_456" not in caplog.text
    assert "SECRET_DESCRIPTION_789" not in caplog.text


@pytest.mark.asyncio
async def test_api_request_and_tool_logs_share_safe_request_id(monkeypatch, caplog):
    # Explicit component isolation; this is not JWT acceptance.
    monkeypatch.setitem(api_main.app.dependency_overrides, api_main.get_current_user,
                        lambda: UserContext(account_id=1, username="component", business_user_id="user_002"))
    server = MCPToolServer()

    async def observed_read(secret: str = ""):
        return {"success": True, "description": secret}

    server.register_tool(
        ToolDefinition(
            name="knowledge_search",
            description="test",
            input_schema={"type": "object"},
            handler=observed_read,
        )
    )
    executor = InstrumentedToolExecutor(
        server,
        runtime_metrics=api_main.runtime_metrics,
    )
    monkeypatch.setattr(api_main, "tool_executor", executor)
    before = api_main.runtime_metrics.snapshot()

    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.INFO, logger="tracing.observability"):
            response = await client.post(
                "/api/tools/execute",
                headers={"X-Request-ID": "REQ-test-123"},
                json={
                    "name": "knowledge_search",
                    "arguments": {"secret": "SECRET_DESCRIPTION_789"},
                },
            )
            invalid = await client.get(
                "/health",
                headers={"X-Request-ID": "bad id"},
            )
            concurrent = await asyncio.gather(
                client.post(
                    "/api/tools/execute",
                    headers={"X-Request-ID": "REQ-concurrent-a"},
                    json={"name": "knowledge_search", "arguments": {"secret": "A"}},
                ),
                client.post(
                    "/api/tools/execute",
                    headers={"X-Request-ID": "REQ-concurrent-b"},
                    json={"name": "knowledge_search", "arguments": {"secret": "B"}},
                ),
            )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "REQ-test-123"
    assert invalid.status_code == 200
    assert is_safe_request_id(invalid.headers["X-Request-ID"])
    assert invalid.headers["X-Request-ID"] != "bad id"
    assert any(
        record.__dict__.get("event") == "http_request_complete"
        and record.__dict__.get("request_id") == "REQ-test-123"
        for record in caplog.records
    )
    assert any(
        record.__dict__.get("event") == "tool_execution_complete"
        and record.__dict__.get("request_id") == "REQ-test-123"
        for record in caplog.records
    )
    assert "/api/tools/execute" in caplog.text
    assert "SECRET_DESCRIPTION_789" not in caplog.text

    assert [item.headers["X-Request-ID"] for item in concurrent] == [
        "REQ-concurrent-a",
        "REQ-concurrent-b",
    ]
    tool_request_ids = {
        record.__dict__.get("request_id")
        for record in caplog.records
        if record.__dict__.get("event") == "tool_execution_complete"
    }
    assert {"REQ-test-123", "REQ-concurrent-a", "REQ-concurrent-b"}.issubset(
        tool_request_ids
    )
    after = api_main.runtime_metrics.snapshot()
    assert after["requests"]["total"] - before["requests"]["total"] == 4
    assert after["requests"]["2xx"] - before["requests"]["2xx"] == 4
    assert (
        after["tools"]["knowledge_search"]["calls"]
        - before["tools"].get("knowledge_search", {}).get("calls", 0)
        == 3
    )
    assert get_request_id() is None
    assert is_safe_request_id(request_id_from_header("bad id\ninjected"))


@pytest.mark.asyncio
async def test_raw_invalid_header_and_unmatched_path_are_not_logged(caplog):
    with caplog.at_level(logging.INFO, logger="tracing.observability"):
        status, headers = await _raw_asgi_request(
            api_main.app,
            "/unmatched/SECRET_USER_123?order_id=SECRET_ORDER_456",
            [(b"x-request-id", b"bad id\r\ninjected")],
        )

    request_id = headers[b"x-request-id"].decode("ascii")
    assert status == 404
    assert is_safe_request_id(request_id)
    assert "SECRET_USER_123" not in caplog.text
    assert "SECRET_ORDER_456" not in caplog.text
    assert "bad id" not in caplog.text


def test_runtime_metrics_are_safe_for_concurrent_recording():
    metrics = RuntimeMetrics(["known"])

    def record() -> None:
        metrics.record_request(200, 1.0)
        metrics.record_tool(
            ToolExecutionResult(
                tool_name="known",
                success=True,
                status="completed",
                duration_ms=1.0,
            ),
            tool_name="known",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: record(), range(200)))

    snapshot = metrics.snapshot()
    assert snapshot["requests"]["total"] == 200
    assert snapshot["tools"]["known"]["calls"] == 200
    assert "unknown" not in snapshot["tools"]


def test_recovery_wrapper_records_aggregates_only(monkeypatch):
    summary = {
        "scanned": 5,
        "recovered_completed": 1,
        "released_for_retry": 2,
        "manual_required": 1,
        "skipped": 1,
        "items": [{"secret": "SECRET_USER_123"}],
    }
    monkeypatch.setattr(
        ExecutionReconciler,
        "reconcile_stale",
        lambda _self, *args, **kwargs: summary,
    )
    metrics = RuntimeMetrics()
    reconciler = InstrumentedExecutionReconciler(
        None,
        None,
        runtime_metrics=metrics,
    )

    assert reconciler.reconcile_stale() is summary
    recovery = metrics.snapshot()["recovery"]
    assert recovery["runs"] == 1
    assert recovery["last_run"] == {
        "scanned": 5,
        "recovered_completed": 1,
        "released_for_retry": 2,
        "manual_required": 1,
        "skipped": 1,
    }
    assert "items" not in recovery
    assert "SECRET_USER_123" not in str(metrics.snapshot())


@pytest.mark.asyncio
async def test_unhandled_500_keeps_request_id_and_is_reraised(caplog):
    app = FastAPI()

    @app.get("/boom/{session_id}")
    async def boom(session_id: str):
        raise RuntimeError(f"SECRET_DESCRIPTION_789 {session_id}")

    metrics = RuntimeMetrics()
    install_request_observability(app, metrics)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.INFO, logger="tracing.observability"):
            response = await client.get(
                "/boom/SECRET_USER_123",
                headers={"X-Request-ID": "REQ-test-500"},
            )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "REQ-test-500"
    assert metrics.snapshot()["requests"]["5xx"] == 1
    assert "SECRET_DESCRIPTION_789" not in caplog.text
    assert "SECRET_USER_123" not in caplog.text
    assert get_request_id() is None


@pytest.mark.asyncio
async def test_runtime_metrics_requires_auth_and_hides_legacy_payloads(monkeypatch):
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/metrics/runtime")).status_code == 401
        monkeypatch.setitem(api_main.app.dependency_overrides, api_main.get_current_user,
                            lambda: UserContext(account_id=1, username="component", business_user_id="user_002"))
        legacy = await client.get("/api/metrics")
        runtime = await client.get("/api/metrics/runtime")

    assert legacy.status_code == 403
    assert runtime.status_code == 200
    assert set(runtime.json()) == {"requests", "tools", "recovery", "context"}
    assert all(isinstance(value, (int, float)) for value in runtime.json()["context"].values())
    assert "items" not in runtime.json()["recovery"]


@pytest.mark.asyncio
async def test_tool_executor_exception_is_reraised_without_error_text(monkeypatch, caplog):
    marker = RuntimeError("SECRET_DESCRIPTION_789")

    async def fail(_self, _name, _arguments, _context=None):
        raise marker

    monkeypatch.setattr(ToolExecutor, "execute", fail)
    metrics = RuntimeMetrics()
    executor = InstrumentedToolExecutor(MCPToolServer(), runtime_metrics=metrics)

    with caplog.at_level(logging.INFO, logger="tracing.observability"):
        with pytest.raises(RuntimeError) as raised:
            await executor.execute("user-controlled-tool-SECRET_USER_123", {})

    assert raised.value is marker
    assert "SECRET_DESCRIPTION_789" not in caplog.text
    assert "SECRET_USER_123" not in caplog.text
    assert metrics.snapshot()["tools"]["unknown"]["transport_failure"] == 1


@pytest.mark.asyncio
async def test_instrumented_executor_preserves_result_identity_and_uses_live_duration(monkeypatch):
    result = ToolExecutionResult(
        tool_name="known",
        success=True,
        status="completed",
        duration_ms=999999.0,
    )

    async def return_result(_self, _name, _arguments, _context=None):
        return result

    monkeypatch.setattr(ToolExecutor, "execute", return_result)
    metrics = RuntimeMetrics(["known"])
    server = MCPToolServer()
    server.register_tool(
        ToolDefinition(
            name="known",
            description="test",
            input_schema={"type": "object"},
            handler=lambda: None,
        )
    )
    executor = InstrumentedToolExecutor(server, runtime_metrics=metrics)

    returned = await executor.execute("known", {})

    assert returned is result
    assert metrics.snapshot()["tools"]["known"]["avg_duration_ms"] < 1000


def test_otel_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(otel_config, "_otel_disabled", False)
    monkeypatch.setattr(otel_config, "_tracer", object())
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(
        otel_config,
        "TracerProvider",
        lambda *args, **kwargs: pytest.fail("exporter/provider initialized"),
        raising=False,
    )

    assert otel_config.get_tracer() is None
    otel_config.init_tracer()

    assert otel_config.get_tracer() is None
