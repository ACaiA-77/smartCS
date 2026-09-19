"""Small, process-local runtime observability helpers."""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from contextvars import ContextVar
from threading import Lock
from typing import Any, Iterable, Mapping

from mcp.execution_recovery import ExecutionReconciler
from mcp.tool_execution import ToolExecutionResult, ToolExecutor


logger = logging.getLogger(__name__)
UNKNOWN_TOOL = "unknown"
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_LABEL_RE = re.compile(r"[A-Za-z0-9._:/{}-]+", re.ASCII)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def is_safe_request_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and _REQUEST_ID_RE.fullmatch(value) is not None
    )


def new_request_id() -> str:
    return uuid.uuid4().hex


def request_id_from_header(value: object) -> str:
    return value if is_safe_request_id(value) else new_request_id()


def get_request_id() -> str | None:
    return request_id_var.get()


def _safe_label(value: object, default: str = "unknown") -> str:
    text = value if isinstance(value, str) else default
    return text if 0 < len(text) <= 64 and _LABEL_RE.fullmatch(text) else default


def _safe_request_id() -> str:
    value = get_request_id()
    return value if is_safe_request_id(value) else "-"


def _duration(value: object, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return number if math.isfinite(number) and number >= 0 else max(fallback, 0.0)


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


class RuntimeMetrics:
    """Thread-safe aggregate metrics with bounded tool labels."""

    def __init__(self, registered_tools: Iterable[str] | None = None) -> None:
        self._lock = Lock()
        self._registered_tools: set[str] = set()
        self.register_tools(registered_tools or ())
        self._requests = {"total": 0, "2xx": 0, "4xx": 0, "5xx": 0}
        self._request_duration_ms = 0.0
        self._tools: dict[str, dict[str, int | float]] = {}
        self._recovery = {
            "runs": 0,
            "scanned": 0,
            "recovered_completed": 0,
            "released_for_retry": 0,
            "manual_required": 0,
            "skipped": 0,
        }
        self._last_run: dict[str, int] | None = None

    def register_tools(self, names: Iterable[str]) -> None:
        with self._lock:
            self._registered_tools.update(
                name for name in names if isinstance(name, str) and name
            )

    def register_tool(self, name: str) -> None:
        self.register_tools((name,))

    def _tool_key_locked(self, name: object) -> str:
        return name if isinstance(name, str) and name in self._registered_tools else UNKNOWN_TOOL

    @staticmethod
    def _new_tool_metrics() -> dict[str, int | float]:
        return {
            "calls": 0,
            "transport_success": 0,
            "transport_failure": 0,
            "business_rejections": 0,
            "replays": 0,
            "timeouts": 0,
            "confirmation_required": 0,
            "attempts_total": 0,
            "retry_attempts": 0,
            "_duration_total_ms": 0.0,
        }

    def record_request(self, status_code: int, duration_ms: float) -> None:
        with self._lock:
            self._requests["total"] += 1
            if 200 <= status_code < 300:
                self._requests["2xx"] += 1
            elif 400 <= status_code < 500:
                self._requests["4xx"] += 1
            elif status_code >= 500:
                self._requests["5xx"] += 1
            self._request_duration_ms += _duration(duration_ms)

    def record_tool(
        self,
        result: ToolExecutionResult,
        *,
        tool_name: str | None = None,
        duration_ms: float | None = None,
    ) -> str:
        with self._lock:
            key = self._tool_key_locked(tool_name or getattr(result, "tool_name", None))
            values = self._tools.setdefault(key, self._new_tool_metrics())
            success = bool(getattr(result, "success", False))
            attempts = _count(getattr(result, "attempts", 0))
            duration = _duration(
                getattr(result, "duration_ms", 0.0)
                if duration_ms is None
                else duration_ms
            )
            values["calls"] += 1
            values["transport_success"] += int(success)
            values["transport_failure"] += int(not success)
            payload = getattr(result, "result", None)
            business_rejection = success and isinstance(payload, dict) and payload.get("success") is False
            values["business_rejections"] += int(business_rejection)
            values["replays"] += int(bool(getattr(result, "replayed", False)))
            values["timeouts"] += int(getattr(result, "error_code", None) == "timeout")
            values["confirmation_required"] += int(
                getattr(result, "status", None) == "confirmation_required"
            )
            values["attempts_total"] += attempts
            values["retry_attempts"] += max(attempts - 1, 0)
            values["_duration_total_ms"] += duration
            return key

    def record_tool_exception(
        self,
        tool_name: str | None,
        duration_ms: float,
        attempts: int = 0,
    ) -> str:
        with self._lock:
            key = self._tool_key_locked(tool_name)
            values = self._tools.setdefault(key, self._new_tool_metrics())
            values["calls"] += 1
            values["transport_failure"] += 1
            values["attempts_total"] += _count(attempts)
            values["_duration_total_ms"] += _duration(duration_ms)
            return key

    def record_recovery(self, summary: Mapping[str, Any]) -> None:
        last_run = {
            key: _count(summary.get(key, 0))
            for key in (
                "scanned",
                "recovered_completed",
                "released_for_retry",
                "manual_required",
                "skipped",
            )
        }
        with self._lock:
            self._recovery["runs"] += 1
            for key, value in last_run.items():
                self._recovery[key] += value
            self._last_run = last_run

    @property
    def last_run(self) -> dict[str, int] | None:
        with self._lock:
            return None if self._last_run is None else dict(self._last_run)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            request_total = self._requests["total"]
            requests = {
                **self._requests,
                "avg_duration_ms": (
                    self._request_duration_ms / request_total if request_total else 0.0
                ),
            }
            tools: dict[str, dict[str, int | float]] = {}
            for name, values in self._tools.items():
                calls = int(values["calls"])
                tools[name] = {
                    key: value
                    for key, value in values.items()
                    if not key.startswith("_")
                }
                tools[name]["avg_duration_ms"] = (
                    float(values["_duration_total_ms"]) / calls if calls else 0.0
                )
            return {
                "requests": requests,
                "tools": tools,
                "recovery": {
                    **self._recovery,
                    "last_run": None if self._last_run is None else dict(self._last_run),
                },
            }


def _emit(logger_: logging.Logger, fields: Mapping[str, Any]) -> None:
    safe_fields = dict(fields)
    logger_.info(
        " ".join(f"{key}={value}" for key, value in safe_fields.items()),
        extra=safe_fields,
    )


def _tool_log_fields(
    *,
    tool_name: str,
    operation_type: object,
    risk_level: object,
    status: object,
    error_code: object,
    attempts: object,
    replayed: object,
    duration_ms: object,
    business_rejection: bool,
    exception_type: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "event": "tool_execution_complete",
        "request_id": _safe_request_id(),
        "tool_name": tool_name,
        "operation_type": _safe_label(operation_type),
        "risk_level": _safe_label(risk_level),
        "status": _safe_label(status),
        "error_code": _safe_label(error_code, default=""),
        "attempts": _count(attempts),
        "replayed": bool(replayed),
        "duration_ms": round(_duration(duration_ms), 3),
        "business_rejection": business_rejection,
    }
    if exception_type:
        fields["exception_type"] = _safe_label(exception_type)
    return fields


class InstrumentedToolExecutor(ToolExecutor):
    """Delegate to :class:`ToolExecutor` and add bounded runtime telemetry."""

    def __init__(
        self,
        *args: Any,
        runtime_metrics: RuntimeMetrics | None = None,
        runtime_logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.runtime_metrics = runtime_metrics or RuntimeMetrics()
        self.runtime_logger = runtime_logger or logger
        self.runtime_metrics.register_tools(
            item["name"] for item in self.server.list_tools()
        )

    async def execute(self, name: str, arguments: dict[str, Any], context=None):
        started = time.perf_counter()
        tool = self.server.get_tool(name)
        registered_name = tool.name if tool is not None else UNKNOWN_TOOL
        if tool is not None:
            self.runtime_metrics.register_tool(tool.name)
        operation_type = getattr(tool, "operation_type", "unknown")
        risk_level = getattr(tool, "risk_level", "unknown")
        try:
            result = await super().execute(name, arguments, context)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            safe_name = self.runtime_metrics.record_tool_exception(
                registered_name,
                duration_ms,
                attempts=0,
            )
            _emit(
                self.runtime_logger,
                _tool_log_fields(
                    tool_name=safe_name,
                    operation_type=operation_type,
                    risk_level=risk_level,
                    status="exception",
                    error_code="internal_error",
                    attempts=0,
                    replayed=False,
                    duration_ms=duration_ms,
                    business_rejection=False,
                    exception_type=type(exc).__name__,
                ),
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        safe_name = self.runtime_metrics.record_tool(
            result,
            tool_name=registered_name,
            duration_ms=duration_ms,
        )
        payload = getattr(result, "result", None)
        _emit(
            self.runtime_logger,
            _tool_log_fields(
                tool_name=safe_name,
                operation_type=getattr(result, "operation_type", operation_type),
                risk_level=getattr(result, "risk_level", risk_level),
                status=getattr(result, "status", "unknown"),
                error_code=getattr(result, "error_code", None),
                attempts=getattr(result, "attempts", 0),
                replayed=getattr(result, "replayed", False),
                duration_ms=duration_ms,
                business_rejection=(
                    bool(getattr(result, "success", False))
                    and isinstance(payload, dict)
                    and payload.get("success") is False
                ),
            ),
        )
        return result


class InstrumentedExecutionReconciler(ExecutionReconciler):
    """Delegate reconciliation and retain aggregate-only recovery metrics."""

    def __init__(self, *args: Any, runtime_metrics: RuntimeMetrics | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.runtime_metrics = runtime_metrics or RuntimeMetrics()

    def reconcile_stale(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        summary = super().reconcile_stale(*args, **kwargs)
        self.runtime_metrics.record_recovery(summary)
        return summary


class _RequestObservabilityMiddleware:
    def __init__(self, app: Any, runtime_metrics: RuntimeMetrics) -> None:
        self.app = app
        self.runtime_metrics = runtime_metrics

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        request_id = request_id_from_header(
            headers.get(b"x-request-id", b"").decode("latin-1") or None
        )
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        completed = False
        status_code = 500

        async def send_with_correlation(message: dict[str, Any]) -> None:
            nonlocal completed, status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            elif (
                message.get("type") == "http.response.body"
                and not message.get("more_body", False)
                and not completed
            ):
                completed = True
                self._complete(scope, status_code, started, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
            if not completed:
                self._complete(scope, status_code, started, request_id)
        except Exception:
            if not completed:
                self._complete(scope, status_code, started, request_id)
            raise
        finally:
            request_id_var.reset(token)

    def _complete(
        self,
        scope: dict[str, Any],
        status_code: int,
        started: float,
        request_id: str,
    ) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        self.runtime_metrics.record_request(status_code, duration_ms)
        _emit(
            logger,
            {
                "event": "http_request_complete",
                "request_id": request_id,
                "method": _safe_label(_header_method(scope)),
                "route_template": _route_template(scope),
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
            },
        )


def install_request_observability(app: Any, runtime_metrics: RuntimeMetrics) -> None:
    """Install an outer ASGI wrapper so generated 500s keep the correlation ID."""

    app.middleware_stack = app.build_middleware_stack()
    app.middleware_stack = _RequestObservabilityMiddleware(
        app.middleware_stack,
        runtime_metrics,
    )


def _header_method(scope: dict[str, Any]) -> str:
    return scope.get("method", "unknown")


def _route_template(scope_or_request: Any) -> str:
    scope = getattr(scope_or_request, "scope", scope_or_request)
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path.startswith("/") else "<unmatched>"


__all__ = [
    "InstrumentedExecutionReconciler",
    "InstrumentedToolExecutor",
    "RuntimeMetrics",
    "get_request_id",
    "install_request_observability",
    "is_safe_request_id",
    "new_request_id",
    "request_id_from_header",
    "request_id_var",
]
