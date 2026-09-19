"""Opt-in real-process crash acceptance with MySQL and isolated demo business data.

Run: python -m scripts.verify_checkpoint_restart
Uses a deterministic test LLM, not the paid provider. No production fault hooks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


async def child(phase: str, directory: str, session_id: str) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    from agents.orchestrator import create_chat_orchestrator
    from checkpoint.store import CheckpointStore
    from langchain_core.messages import HumanMessage
    from memory.long_term import LongTermMemory
    from memory.session_store import SessionStore
    from memory.short_term import ShortTermMemory
    from mcp.approval_store import ApprovalService
    from mcp.execution_ledger import ExecutionLedger
    from mcp.execution_recovery import ExecutionReconciler
    from mcp.mcp_server import MCPToolServer, create_default_tools
    from mcp.order_repository import OrderRepository
    from mcp.tool_execution import ToolExecutor
    from refunds.service import RefundService
    from tickets.service import TicketService
    from tests.conftest import MockLLM

    store = CheckpointStore.from_env()
    await store.initialize()
    if phase == "cleanup":
        async with store.session_lock(session_id):
            snapshot = await store.load(session_id, "user_002")
            if snapshot:
                await store.delete(session_id, "user_002", snapshot.version)
        return

    repository = OrderRepository(str(Path(directory) / "orders.db"))
    ledger = ExecutionLedger(repository.db_path)
    memory = LongTermMemory()
    short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")
    sessions = SessionStore(short)
    server = create_default_tools(MCPToolServer(), long_term_memory=memory, order_repository=repository)
    executor = ToolExecutor(server, ledger=ledger, approval_service=ApprovalService(repository.db_path))
    llm = MockLLM()
    orchestrator = create_chat_orchestrator(
        llm=llm, session_store=sessions, long_term_memory=memory,
        mcp_server=server, tool_executor=executor, checkpoint_store=store,
    )
    if phase in {"confirm_crash", "effect_crash"}:
        complete = ledger.complete
        def exit_after_commit(key, result):
            if phase == "effect_crash":
                print(json.dumps({"phase": phase, "business_committed_ledger_in_progress": True}), flush=True)
                os._exit(75)
            complete(key, result)
            print(json.dumps({"phase": phase, "business_and_ledger_committed": True}), flush=True)
            os._exit(74)
        ledger.complete = exit_after_commit
    if phase == "recover":
        recovery = ExecutionReconciler(ledger, RefundService(repository), TicketService(repository))
        summary = recovery.reconcile_stale(stale_after_seconds=0.001)
        print(json.dumps({"reconciled_completed": summary["recovered_completed"]}), flush=True)
        tool = server.get_tool("refund_create")
        def forbidden_write(**_kwargs):
            raise AssertionError("recovery called the refund WRITE handler again")
        tool.handler = forbidden_write

    message = "帮我退款 ORD-20260801-0002" if phase == "prepare" else "确认退款"
    request_id = "prepare-1" if phase == "prepare" else "confirm-1"
    result = await orchestrator.ainvoke({
        "session_id": session_id, "user_id": "user_002", "messages": [HumanMessage(content=message)],
        "client_request_id": request_id, "intent": "", "sub_results": {},
        "compliance_passed": True, "final_response": "", "needs_clarification": False,
    })
    checkpoint = await store.load(session_id, "user_002")
    refunds = repository.get_order("ORD-20260801-0002")["refunds"]
    if phase == "prepare":
        assert checkpoint.current_stage == "WAIT_CONFIRM" and checkpoint.pending_action
        assert len(refunds) == 0
    else:
        assert "退款申请已提交" in result["final_response"]
        assert len(refunds) == 1
        assert checkpoint.pending_action is None
    print(json.dumps({"phase": phase, "pid": os.getpid(), "stage": checkpoint.current_stage,
                      "refund_count": len(refunds), "llm_calls": llm.call_count,
                      "response": result["final_response"]}, ensure_ascii=False), flush=True)
    if phase == "prepare":
        os._exit(73)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase")
    parser.add_argument("--directory")
    parser.add_argument("--session")
    args = parser.parse_args()
    os.environ.update(OTEL_SDK_DISABLED="true", EMBEDDING_BACKEND="hash", PYTHONIOENCODING="utf-8")
    if args.phase:
        asyncio.run(child(args.phase, args.directory, args.session))
        return
    with tempfile.TemporaryDirectory(prefix="smartcs-checkpoint-") as directory:
        def run(phase):
            return subprocess.run(
                [sys.executable, "-m", "scripts.verify_checkpoint_restart", "--phase", phase,
                 "--directory", case_directory, "--session", session_id],
                capture_output=True, text=True, encoding="utf-8", timeout=90,
            )
        results = []
        for crash, crash_exit in [("confirm_crash", 74), ("effect_crash", 75)]:
            session_id = "restart-acceptance-" + uuid.uuid4().hex[:12]
            case_directory = str(Path(directory) / crash)
            try:
                for phase, expected_exit in [("prepare", 73), (crash, crash_exit), ("recover", 0), ("replay", 0)]:
                    result = run(phase)
                    if result.returncode != expected_exit:
                        raise AssertionError(f"{phase}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
                    results.append({"case": crash, "phase": phase, "exit_code": result.returncode, "output": result.stdout.strip()})
            finally:
                cleanup = run("cleanup")
                if cleanup.returncode:
                    print("Test checkpoint cleanup failed; session=" + session_id, file=sys.stderr)
        print(json.dumps({"passed": True, "real_process_exit": True, "llm": "deterministic test double",
                          "redis": "unavailable test port", "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
