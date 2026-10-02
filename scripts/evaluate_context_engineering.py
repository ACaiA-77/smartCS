"""Exercise durable context assembly on isolated UUID MySQL test sessions.

This is a local infrastructure benchmark, NOT a live-LLM quality or SLA claim.
It never invokes an embedding model, business write tool, or an LLM. Redis loss
is simulated by deleting only this benchmark's own working-set key, never by
FLUSHDB/FLUSHALL on a shared service. Raw events remain append-only afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from checkpoint.models import AgentCheckpoint, CheckpointOwnershipError
from checkpoint.store import CheckpointStore
from context.manager import ContextManager, WorkingSetCache
from context.models import ModelProfile
from memory.session_store import ConversationState
from memory.short_term import ShortTermMemory


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))]


def _all_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _all_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_values(child)
    else:
        yield value


async def evaluate(*, turns: int, output_root: Path, require_redis: bool = False) -> dict:
    if turns not in (50, 100):
        raise ValueError("turns must be 50 or 100")
    if output_root.exists():
        raise FileExistsError("refusing to overwrite prior benchmark evidence")
    load_dotenv()
    store = CheckpointStore.from_env()
    await store.initialize()
    short = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    cache = WorkingSetCache(short, ttl=1800)
    profile = ModelProfile(
        name="kimi-k2.7-code", provider="moonshot", context_limit=8192,
        max_output_tokens=1024, reserve=256, safety_margin=512,
    )
    manager = ContextManager(event_store=store, cache=cache, default_model=profile)
    sid = "context-bench-" + uuid.uuid4().hex
    uid = "context-owner-" + uuid.uuid4().hex
    order_id = "ORD-CONTEXT-" + uuid.uuid4().hex[:12].upper()
    key = "context-refund-" + uuid.uuid4().hex
    pending = {
        "type": "refund_create", "order_id": order_id, "user_id": uid,
        "amount": 598.00, "refund_mode": "refund_only", "reason": "benchmark fixture only",
        "idempotency_key": key,
        "arguments": {"order_id": order_id, "user_id": uid, "reason": "benchmark fixture only"},
    }
    rows = []
    checkpoint = None
    original_event_bytes: dict[int, str] = {}
    expected_history: list[dict[str, str]] = []
    try:
        async with store.session_lock(sid):
            for turn in range(1, turns + 1):
                text = f"Fixture conversation turn {turn}; order={order_id}, amount=598.00. " + "历史客服上下文 " * 60
                session = ConversationState(
                    last_intent="conversation", turn_count=turn,
                    accumulated_entities={"order_id": order_id}, pending_action=pending,
                ).to_dict()
                workflow = {
                    "intent": "conversation", "sub_results": {}, "compliance_passed": True,
                    "final_response": "", "current_agent": "orchestrator", "needs_clarification": False,
                }
                cp = AgentCheckpoint(
                    session_id=sid, user_id=uid, version=checkpoint.version if checkpoint else 0,
                    messages=[{"role": "user", "content": text}], pending_action=pending,
                    context={"workflow_version": 1, "request_id": f"fixture-{turn}",
                             "request_hash": hashlib.sha256(text.encode()).hexdigest(),
                             "session_state": session, "state": workflow},
                )
                checkpoint = await (store.update(cp) if checkpoint else store.save(cp))
                expected_history.append({"role": "user", "content": text})
                if turn == 1:
                    before = await store.recent_events(sid, uid, after_seq=0, limit=1000)
                    original_event_bytes = {int(row["seq"]): hashlib.sha256(
                        json.dumps(row, sort_keys=True, default=str).encode()).hexdigest() for row in before}
                synchronize = getattr(manager, "synchronize_checkpoint", None)
                if callable(synchronize):
                    await synchronize(checkpoint)
                cold_probe = turn == turns // 2
                if cold_probe:
                    await cache.invalidate(sid, uid)
                    manager = ContextManager(event_store=store, cache=cache, default_model=profile)
                state = {**workflow, "session_id": sid, "user_id": uid,
                         "client_request_id": f"fixture-{turn}", "messages": [HumanMessage(content=text)],
                         "sub_results": {"_session_context": session}}
                start = time.perf_counter()
                async with manager.bind_request(sid, uid, request_id=f"fixture-{turn}", state=state):
                    package = await manager.build(
                        sid, uid, "conversation", text, profile, state=state,
                        system_prompt="SmartCS deterministic context benchmark. Do not execute business actions.",
                    )
                elapsed = (time.perf_counter() - start) * 1000
                assert package.total_tokens <= profile.prompt_budget, "assembled context exceeds budget"
                if turn >= 3:
                    block_tokens = package.diagnostics.get("block_tokens", {})
                    assert block_tokens.get("RecentHistory", 0) > 0, "durable recent history never reached model context"
                exact = list(_all_values(package.protected_fields))
                assert order_id in exact and key in exact and 598.00 in exact
                serialized = json.dumps(package.protected_fields, ensure_ascii=False)
                assert uid in serialized and "pending_action" in serialized
                row = {"turn": turn, "context_tokens_total": package.total_tokens,
                       "latency_ms": round(elapsed, 3), "cold_cache_probe": cold_probe,
                       "diagnostics": package.diagnostics}
                rows.append(row)
                reply = {"role": "assistant", "content": f"Fixture response {turn}"}
                finished = AgentCheckpoint.model_validate({
                    **checkpoint.model_dump(), "current_stage": "WAIT_CONFIRM", "status": "waiting",
                    "messages": [*(message.model_dump() for message in checkpoint.messages), reply],
                    "context": {**checkpoint.context, "state": {**workflow, "final_response": reply["content"]}},
                })
                expected_history.append(reply)
                checkpoint = await store.update(finished)
            history = await store.history(sid, uid)
            assert len(history) == 2 * turns, "durable conversation lost messages"
            assert [{"role": message["role"], "content": message["content"]} for message in history] == expected_history
            events = await store.recent_events(sid, uid, after_seq=0, limit=1000)
            final_event_bytes = {int(row["seq"]): hashlib.sha256(
                json.dumps(row, sort_keys=True, default=str).encode()).hexdigest() for row in events}
            assert all(final_event_bytes.get(seq) == fingerprint for seq, fingerprint in original_event_bytes.items()), \
                "context assembly mutated original durable events"
            try:
                await store.history(sid, "another-owner")
            except CheckpointOwnershipError:
                isolated = True
            else:
                raise AssertionError("history leaked to another owner")
        latencies = [row["latency_ms"] for row in rows]
        cache_backends = {row["diagnostics"].get("working_set_cache_backend") for row in rows}
        cache_is_real_redis = all(bool(row["diagnostics"].get("working_set_cache_is_real_redis")) for row in rows)
        cache_hits = sum(1 for row in rows if row["diagnostics"].get("working_set_cache_hit"))
        cold_probe_turn = next((row["turn"] for row in rows if row["cold_cache_probe"]), None)
        rewarmed_hit = (
            any(row["turn"] == cold_probe_turn + 1 and row["diagnostics"].get("working_set_cache_hit")
               for row in rows)
            if cold_probe_turn is not None else False
        )
        if require_redis:
            if cache_backends != {"redis"} or not cache_is_real_redis:
                raise AssertionError("--require-redis: benchmark did not run on real Redis")
            if cache_hits < turns - 2:
                raise AssertionError("--require-redis: warm path did not consistently hit Redis")
            if cold_probe_turn is None or not rewarmed_hit:
                raise AssertionError("--require-redis: cold restore was not re-warmed into Redis")
        report = {
            "status": "PASS", "scope": "real MySQL + configured cache transport + deterministic text fixtures",
            "live_llm_called": False, "business_tools_called": False, "embedding_backend_changed": False,
            "turns": turns, "history_messages": len(history), "owner_isolation": isolated,
            "lossless_pending_action": True, "event_count": len(events),
            "working_set_cache_backend": sorted(str(backend) for backend in cache_backends),
            "working_set_cache_is_real_redis": cache_is_real_redis,
            "working_set_cache_hits": cache_hits, "working_set_cache_misses": turns - cache_hits,
            "cold_restore_turn": cold_probe_turn, "rewarmed_redis_hit_after_cold_restore": rewarmed_hit,
            "prompt_budget": profile.prompt_budget, "peak_context_tokens": max(row["context_tokens_total"] for row in rows),
            "build_latency_ms": {"median": statistics.median(latencies), "p95": _percentile(latencies, .95)},
            "tokenizer_precision": rows[-1]["diagnostics"].get("tokenizer_precision"),
            "cache_loss_method": "delete benchmark-owned working-set key and create a fresh ContextManager",
            "shared_redis_flushed": False, "rows": rows,
            "limits": ["No production SLA inferred", "No online model-quality measurement",
                       "Cache transport must be read from diagnostics; process-local fallback is not real Redis",
                       "Provider-native token precision depends on configured tokenizer"],
        }
        output_root.mkdir(parents=True)
        (output_root / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        await cache.invalidate(sid, uid)
        # Remove only the benchmark cursor/receipts through the approved logical
        # clear path. Immutable test events are retained by design, never dropped.
        async with store.session_lock(sid):
            last = await store.load(sid, uid)
            if last is not None:
                if last.status == "running":
                    terminal = AgentCheckpoint.model_validate({**last.model_dump(), "current_stage": "WAIT_CONFIRM",
                                                               "status": "waiting"})
                    last = await store.update(terminal)
                await store.delete(sid, uid, last.version)
        redis_client = getattr(short, "_redis", None)
        if redis_client is not None:
            await redis_client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, choices=(50, 100), default=100)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--real-mysql", action="store_true", help="explicitly opt into UUID-scoped local database writes")
    parser.add_argument("--require-redis", action="store_true",
                        help="fail closed unless every build used the configured real Redis backend")
    args = parser.parse_args()
    if not args.real_mysql:
        parser.error("--real-mysql is required; this command writes isolated test events")
    report = asyncio.run(evaluate(turns=args.turns, output_root=args.output_root, require_redis=args.require_redis))
    print(json.dumps({key: report[key] for key in ("status", "turns", "history_messages", "peak_context_tokens",
                                                  "build_latency_ms", "tokenizer_precision",
                                                  "working_set_cache_backend", "working_set_cache_is_real_redis",
                                                  "working_set_cache_hits", "rewarmed_redis_hit_after_cold_restore")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
