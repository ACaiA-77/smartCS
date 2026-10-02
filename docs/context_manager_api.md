# ContextManager API

`context.ContextManager` is the single prompt-building and model-invocation boundary for agents. It is deliberately separate from durable event/checkpoint storage and user-memory ownership.

## Constructing and binding

```python
from context import ContextManager, ModelProfile, WorkingSetCache
from memory.short_term import ShortTermMemory

profile = ModelProfile.from_env(model="configured-model", provider="configured-provider")
cache = WorkingSetCache(ShortTermMemory(ttl_seconds=1800), ttl=1800)
context_manager = ContextManager(
    event_store=checkpoint_store,
    cache=cache,
    user_memory=user_memory_service,
    default_model=profile,
)

async with context_manager.bind_request(session_id, user_id, request_id, state) as request:
    # Existing outer request scopes may call ContextManager.invoke without
    # creating a second scope; tool event counters and execution identity persist.
    ...
```

The positional `bind_request(session_id, user_id, request_id, state)` form is supported. Optional `last_event_seq` and `checkpoint_version` keywords let callers supply trusted checkpoint cursors. Scope cleanup always resets `active_context` in `finally`.

## Building and invoking

```python
package = await context_manager.build(
    session_id, user_id, agent, current_message, model,
    state=state,
    system_prompt=stable_system_prefix,
    task_message=final_task_text,
    evidence=tool_or_rag_evidence,
    tool_schemas=schemas,
)
response = await context_manager.invoke(
    llm, agent, messages, state=state,
    session_id=session_id, user_id=user_id,
    task_message=final_task_text, evidence=tool_or_rag_evidence,
    tool_schemas=schemas,
)
```

`ContextPackage` returns the actual LangChain `messages`, `total_tokens`, `block_tokens`, protected fields, and per-build diagnostics. The assembler counts the final assembled messages (including role/message framing) and enforces `total_tokens <= ModelProfile.prompt_budget`; required P0/P1 overflow fails closed. The system prefix and policy rules precede separately tagged tool schemas; dynamic blocks follow policy physical order. Priority is independent of physical order.

User profile cards use `UserProfileCards` (P2); query-retrieved episodic hints use `RetrievedUserMemory` (P4). Current task Tool/RAG `Evidence` is P2 and wins equal-priority budget eviction over profile cards. Retrieved memory cannot displace the current authoritative evidence.

The `knowledge_rag.rewrite` and `compliance_checker` policies are query-only: they include only their current task, without user memory, session state, retrieved history, tool previews, or status blocks, and skip cache/event-store hydration. If request state contains only the current message, `RecentHistory` is sourced from the owner-scoped working set; only the final current user event is excluded, so earlier identical confirmations remain distinct. Up to eight original messages are considered, with a four-message floor during prompt-only history compaction.

## Checkpoint/cache integration

- `await context_manager.synchronize_checkpoint(cp)` updates owner-scoped cached messages, workflow state, event cursor, and checkpoint version from an already-authoritative checkpoint. It does **not** query MySQL. A cold cache is marked for one cold restore because a checkpoint alone cannot provide the session digest or recent tool previews.
- `await context_manager.invalidate_session(session_id, user_id)` removes exactly that owner's context cache key and local ephemeral state; use it when clearing API session history.
- `WorkingSetCache` uses a sliding 1800-second TTL by default. `ShortTermMemory` fallback is process-local and explicitly reported as `process_fallback`, not Redis.
- Hot builds use a valid owner-scoped working set and trusted checkpoint cursor/version without querying the event store. A cache miss, expired entry, or stale cursor restores from the event store.
- Recent tool evidence may use `recent_events` (latest bounded N). Rolling-summary compaction instead reads `events_range(after_seq, before_seq, limit)` in ascending contiguous pages. `summary_event_seq` advances only to the last event actually processed; a page gap does not skip forward.
- Durable archive facts and provenance are retained in storage. The P3 `Summary` prompt block is a separate bounded preview (at most 600 tokenizer-counted text tokens before block framing); long archive contents are not serialized wholesale into every prompt.

## Tool events and diagnostics

`record_tool_call(name, arguments)` and `record_tool_result(result)` append durable event records before updating cache previews. Their event keys are per-execution attempt keys; they do not replace or modify business idempotency keys. Full tool payloads remain in the event store; only bounded previews and exact protected identifiers enter prompt context.

`metrics_snapshot()` returns aggregate numeric-only counters with no owner IDs, prompts, tool payloads, or model labels. Per-build tokenizer details and compaction diagnostics are available on the returned package rather than in that aggregate snapshot.

## Tokenizer limits

`tiktoken` is required unless an explicit tokenizer is injected. There is no character-count fallback. Unknown model mappings use a named tiktoken encoding estimate; Moonshot/Kimi native tokenization and provider framing are explicitly marked unverified. `ModelProfile` subtracts output, reserve, and safety tokens before packing. Runtime configuration uses `MODEL_NAME`, `MODEL_PROVIDER`, and `SMARTCS_CONTEXT_LIMIT`, `SMARTCS_CONTEXT_MAX_OUTPUT_TOKENS`, `SMARTCS_CONTEXT_RESERVE`, `SMARTCS_CONTEXT_SAFETY_MARGIN`, `SMARTCS_CONTEXT_SOFT_RATIO`, `SMARTCS_CONTEXT_HARD_RATIO`, `SMARTCS_CONTEXT_RECENT_MESSAGES`, `SMARTCS_CONTEXT_USER_MEMORY_TOP_K`, `SMARTCS_CONTEXT_EVIDENCE_TOP_K`, `SMARTCS_CONTEXT_TOOL_PREVIEW_CHARS`, and `SMARTCS_CONTEXT_COMPRESSION_ATTEMPTS`. Deployments must set realistic provider limits and safety margins; this code does not claim server-side tokenizer equivalence or certify a provider's context limit.

`history_context_text(history, max_tokens=None, model=None)` is the shared tokenizer-backed formatter for legacy short-term history reads. It accepts a model name or a validated `ModelProfile`.

## Background user-memory worker

`memory.user_memory_worker.UserMemoryWorker` owns deferred candidate application; the chat request tail only enqueues (`orchestrator._process_user_memory` performs a bounded idempotent `UserMemoryService.process_message` and never awaits application).

- FastAPI lifespan starts/stops the worker (`api/main.py`); `stop()` wakes the poll loop via an internal event, waits one bounded cycle, then cancels.
- Each cycle sweeps durable owners with `UserMemoryService.pending_user_ids(limit)` (PENDING or claim-expired rows) and processes each owner with `process_pending(user_id, limit)`; the claim uses the repository lease (`claim_token`/`claim_until`), so concurrent workers and crash recovery are exactly-once per candidate.
- Polling is 2s while work exists and backs off exponentially (15s→60s cap) when idle. `run_once()` is public for deterministic single-cycle driving in tests/ops.
- `worker.stats()` is numeric-only (cycles, users_swept, claimed/accepted/failed totals, last error type); failures log types, never memory contents, and never replay model/tool/business execution.

## Cache backend diagnostics

`working_set_cache_backend` / `working_set_cache_is_real_redis` in build diagnostics report the transport that build actually used: they are read after the cache get/put attempt, so the first build after process start correctly reports `redis` once the lazy client connects. `evaluate_context_engineering.py --require-redis` fails the whole run unless every build used the configured real Redis, the warm path consistently hits it, and the forced cold restore is re-warmed into a subsequent Redis hit.
