"""Phase 4 legacy-side probe: run comparison scenarios through the REAL legacy stack.

This is a *wrapper*, not a reimplementation. Every decision below is produced by
the existing `evals/` infrastructure:

    evals.scenarios.build_runtime()  -> OrderRepository + MCPToolServer +
                                        ExecutionLedger + ApprovalService +
                                        ToolExecutor + ChatOrchestrator +
                                        DeterministicEvalLLM
    runtime.orchestrator.ainvoke()   -> the production legacy chat path
    runtime.executor.calls           -> ObservedExecutor's record of every tool
                                        call the orchestrator actually made

Nothing about routing, authorization or tool selection is re-derived here; we
only observe. That matters because the whole point of Phase 4 is to compare the
Pi harness against the legacy behaviour as it actually is.

Usage:
    python -m scripts.legacy_probe < scenarios.json   (run from python-impl)
    OR  python pi-harness/scripts/legacy_probe.py < scenarios.json   (sets sys.path)

stdin : {"scenarios": [{"id","user_id","turns":["...","..."]}, ...]}
stdout: {"results": [{"id", "turns":[{...}], "write_tools":[...], ...}]}
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PYTHON_IMPL = Path(__file__).resolve().parents[2] / "python-impl"
if str(PYTHON_IMPL) not in sys.path:
    sys.path.insert(0, str(PYTHON_IMPL))

WRITE_TOOLS = {"refund_create", "refund_confirm", "ticket_create"}


def _count(runtime: Any, table: str) -> int:
    with runtime.repository.transaction() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


async def _run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    from evals.scenarios import _state, build_runtime

    runtime = build_runtime()
    try:
        user_id = scenario.get("user_id", "user_002")
        # The demo database ships with pre-existing rows, so only deltas are
        # meaningful; a raw COUNT would look like "22 refunds created".
        refunds_before = _count(runtime, "refunds")
        tickets_before = _count(runtime, "support_tickets")
        session_id = f"legacy-{scenario['id']}"
        turns: list[dict[str, Any]] = []
        write_calls: list[dict[str, Any]] = []

        for index, message in enumerate(scenario["turns"]):
            before = len(runtime.executor.calls)
            await runtime.orchestrator.ainvoke(
                _state(message, user_id=user_id, session_id=session_id)
            )
            new_calls = runtime.executor.calls[before:]
            calls = [
                {
                    "name": call["name"],
                    "arguments": {
                        key: value
                        for key, value in (call["arguments"] or {}).items()
                        if key in {"order_id", "user_id", "title", "description", "priority", "category",
                                   "pending_action_id", "client_request_id", "request_payload_hash"}
                    },
                    "success": bool(getattr(call["result"], "success", False)),
                }
                for call in new_calls
            ]
            for call in calls:
                if call["name"] in WRITE_TOOLS:
                    write_calls.append({**call, "turn_index": index})
            turns.append(
                {
                    "index": index,
                    "user": message,
                    "tools": calls,
                    "write_attempts": [c["name"] for c in calls if c["name"] in WRITE_TOOLS],
                    "refunds_after": _count(runtime, "refunds"),
                    "tickets_after": _count(runtime, "support_tickets"),
                }
            )

        return {
            "id": scenario["id"],
            "user_id": user_id,
            "turns": turns,
            # Aggregated decision surfaces the comparison consumes.
            "write_tools": [call["name"] for call in write_calls],
            "write_arguments": {call["name"]: call["arguments"] for call in write_calls},
            "write_turn_indexes": [call["turn_index"] for call in write_calls],
            "refunds_created": _count(runtime, "refunds") - refunds_before,
            "tickets_created": _count(runtime, "support_tickets") - tickets_before,
        }
    finally:
        runtime.close()


async def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    scenarios = payload.get("scenarios", [])
    results = []
    for scenario in scenarios:
        try:
            results.append(await _run_scenario(scenario))
        except Exception as exc:  # a probe failure must be visible, not silently dropped
            results.append({"id": scenario.get("id"), "error": f"{type(exc).__name__}: {exc}"})
    sys.stdout.write(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
