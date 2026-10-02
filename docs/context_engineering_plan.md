# SmartCS context engineering implementation contract

Baseline: `b8b6a0c`. Scope is the previously requested twelve context-engineering requirements. This local iteration must not commit, push, deploy, replace production embeddings, modify retrieval benchmarks/gold, or weaken ToolExecutor and checkpoint write fencing.

## Phase boundaries

1. Add append-only MySQL conversation events and a session digest. Keep checkpoint CAS, advisory locks, completed-request receipts, and legacy snapshot reads. Persist workflow cursors separately from message history. Clear history with a cutoff event, not event deletion.
2. Add an owner-scoped Redis working-set cache with 1800-second sliding TTL. Durable event/digest writes precede cache updates. A cold miss restores digest, recent conversation events, and checkpoint workflow state.
3. Introduce central context assembly and route all existing LLM calls through it. Agent policies declare blocks; no per-Agent history slicing. Keep static prompt prefixes stable and tool schemas separate from dynamic evidence.
4. Implement configurable model budgets, tokenizer counting, priority packing, lossless protected fields, bounded layered compaction, and per-build diagnostics. Required blocks overflow fails closed; never delete durable events to compress context.
5. Separate shared KnowledgeMemory from user-owned profile and episodic memory. Deferred candidate extraction and policy-controlled versioned writes; business state is never an authoritative profile fact.
6. Exercise long sessions, cache loss, identity isolation, compression fidelity, tool-result visibility, profile conflicts, and existing business/recovery regressions. Report measured results and limits, not assumed SLOs.

## Shared integration interfaces

Storage owner: `checkpoint/store.py`, `checkpoint/models.py`, `context/storage.py`, storage-specific tests. The existing CheckpointStore interface stays intact. Add `last_event_seq` to checkpoints and create conversation_event/session_digest during initialize. Append events in the same transaction as checkpoint changes whenever they are derived from checkpoint state. Stable event keys make retry ingestion idempotent. Do not persist conversation history in new checkpoint rows; expose history and request-message restoration from events while reading old snapshots compatibly.

Additional store methods (all async and owner-scoped):

- `append_event(session_id, user_id, event_type, payload, *, event_key=None) -> dict` containing seq, event_type, payload, event_id.
- `history(session_id, user_id) -> list[dict]` with user/assistant role/content, respecting the clear-history cutoff.
- `load_working_set(session_id, user_id, *, recent_limit=8) -> dict` with recent_messages, rolling_summary, archive_summary, session_state, last_event_seq, version, protected_fields, summary_event_seq.
- `save_digest(session_id, user_id, *, rolling_summary=None, archive_summary=None, protected_fields=None, summary_event_seq=None) -> None` (preserve unset fields, validate provenance/cursors).
- `recent_events(session_id, user_id, *, after_seq=0, limit=100) -> list[dict]`, bounded and cutoff-aware; raw event detail remains retrievable without compression loss.

Cache: `WorkingSetCache(short_term_memory, ttl=1800)` exposes async get(session_id,user_id), put(session_id,user_id,working_set), invalidate(session_id,user_id). Keys and payload validate both owners. Use ShortTermMemory transport, including explicitly observable Redis-unavailable fallback; do not label process-local fallback as real Redis.

Context owner: new context modules except storage.py. `ContextManager(event_store=None, cache=None, user_memory=None, ...)` provides `build(session_id,user_id,agent,current_message,model=None, *, state=None, system_prompt='', task_message=None, evidence=None, tool_schemas=None) -> ContextPackage`, `invoke(llm,agent,messages, *, state=None,...)`, `record_tool_call(name,arguments)` and `record_tool_result(result)`. ContextPackage exposes messages, total_tokens, block_tokens, diagnostics, and protected_fields. `active_context` is a request-scoped ContextVar; every binding is reset in finally. Direct Agent use without an active request uses the same centralized policy but no pretend durable storage. Allow cold loading against the storage duck-typed interface above and hot cache refresh after a durable event. Expose a request binding scope so ToolExecutor instrumentation cannot leak between requests.

Memory owner: memory/user_memory.py and knowledge naming compatibility. UserMemoryService provides async initialize, profile_cards(user_id,limit=3), retrieve(user_id,query,top_k=3), process_message(user_id,session_id,event_id,content), and consolidate(user_id). Production storage reuses PlatformDatabase; test repositories are explicitly injected. Preserve versions/source timestamps, inactive old cards, candidate decisions, non-authoritative episodic provenance, and ownership filters. KnowledgeMemory retains all current RAG behavior and LongTermMemory remains a compatible import.

Integration follows these interfaces after reading actual implementations. Preserve the original request hash, checkpoint stage machine, business identity mapping, write plans, execution ledger, tool arguments, RAG queries/rewrite/retrieval traces, and public authorization behavior. API history reads the event log once available. Tests must distinguish deterministic fixtures from real MySQL/Redis and model integration.

## Completion evidence

Finish locally with READY_FOR_REVIEW and A-F material: actual files; models/migration; warm/cold/context/compression/memory call chains; tests/benchmark; the twelve requirements mapped to evidence; explicit limits/deferred work. Readiness is for independent review, not a production deployment or security/SLA certification.
