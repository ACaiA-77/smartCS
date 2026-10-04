# SmartCS Agent Harness (pi-harness)

Node 22 / TypeScript. The **agent runtime**: it owns the agent loop, the
transcript, tool wiring and the browser-facing chat edge.

```text
Browser → pi-harness (this package)          ← decides what it WANTS to do
                 ↓  internal HTTP + per-turn Service JWT
          python-impl (Business Runtime)     ← decides whether it is ALLOWED
```

## The boundary rule

This is the design's load-bearing invariant, enforced by construction rather
than by convention:

> The agent decides *what it wants to do*; the Python runtime decides *whether
> it is allowed to happen*.

Concretely, this package:

- never persists business state (no orders, refunds, tickets or ledger);
- never mints trusted identity — it forwards the user's own token to Python and
  lets Python resolve `account → business_user` (single authority);
- never treats the model transcript as a business fact;
- never invents an outcome for a write. When the response to a write is lost it
  asks the ledger (`/internal/operation_status`) instead of retrying.

Full rules: [`../python-impl/docs/runtime-boundaries.md`](../python-impl/docs/runtime-boundaries.md).

## Quick start

```bash
npm ci                 # NOT `npm install`: the Pi version is lockfile-frozen
cp ../python-impl/.env .env    # (optional) model credentials are read from there
npm run dev            # starts the HTTP edge on :8971
npm test               # vitest
npm run typecheck      # tsc --noEmit
```

`npm ci` is intentional. The Pi package version (1.0.1) and its transitive
dependencies are frozen by `package-lock.json`; migrating to a newer Pi release
is a reviewed change, never an automatic upgrade.

Configuration is explicit by design: the Pi session dir, runtime cwd and agent
dir are all named (`SMARTCS_RUNTIME_CWD`, `SMARTCS_PI_SESSION_DIR`,
`SMARTCS_PI_AGENT_DIR`), never derived from `~/.pi`. Discovery of user-level
extensions, skills, prompts and context files is switched off.

## Layout

| Path | Responsibility |
|---|---|
| `src/agent/` | assembly (`create-smartcs-agent.ts`), system prompt, tool shells, extensions (audit, compliance, context injection) |
| `src/session/` | `registry.ts` (per-session single-writer mutex), `pi-session.ts` (先查后建), `outbox.ts` (durable memory dispatcher) |
| `src/business/` | Python internal client, service-JWT minting, per-turn identity holder |
| `src/db/` | MySQL repositories: `agent_run_receipt`, `memory_source_event`, outbox recovery |
| `src/history/` | harness-aware history projector for the web client |
| `src/streaming/` | status channel, run output buffer, SSE framing |
| `src/tracing/` | OpenTelemetry spans + the bounded audit queue/dispatcher |
| `src/server/` | HTTP edge: `/api/chat`, `/api/chat/stream`, `/internal/history/*` |
| `tests/` | vitest; `tests/python/` holds Python-side acceptance for `internal_api` |

## The tool face

The whitelist passed to the SDK **is** the tool face — a tool an extension
registered but that is not whitelisted does not exist for the model.

| Tool | Kind | Transport |
|---|---|---|
| `order_query` | READ | internal HTTP |
| `knowledge_search` | READ | HTTP (default) or MCP |
| `ticket_query` | READ | internal HTTP |
| `refund_evaluate` | READ | internal HTTP |
| `risk_check` | READ | internal HTTP |
| `refund_confirm` | WRITE | internal HTTP (live only) |
| `ticket_create` | WRITE | internal HTTP (live only) |

Model-visible schemas declare **business parameters only**. `user_id`,
`account_id`, `session_id`, `client_request_id`, `request_payload_hash` and
`confirmed` belong to the runtime: Python strips any that arrive and re-binds
them from the verified service claims, so a model-supplied value is inert. The
system prompt is composed from the same switch that mounts the write tools, so
it can never advertise a tool the model does not have.

### Transport split (deliberate, final)

`knowledge_search` is the only tool that can move behind MCP: it searches a
shared corpus with no per-user partitioning, so the channel needs only a
server-level token. The identity-bound tools stay on the internal HTTP
envelope, which carries a per-turn identity. That split is the design's final
boundary, not a migration in progress:

```text
shared, no user identity   → MCP
per-turn identity / WRITE  → internal HTTP
```

Enable the MCP path with the compose profile:

```bash
SMARTCS_MCP_TOKEN=$(openssl rand -hex 24) \
SMARTCS_KNOWLEDGE_TRANSPORT=mcp \
docker compose --profile mcp up -d
```

## Write modes

`SMARTCS_WRITE_MODE` selects what the write tools do — `off` (default),
`shadow` (plan recorded, nothing executed, no HTTP), `live` (the runtime
authorizes and executes). `live` refuses to start without the durable receipt
store, because that log is the only way to reconcile a dropped write response.

## Memory outbox

The user's raw message is durable in `memory_source_event` *before* the model
runs. `MemoryOutboxDispatcher` (started by `src/server/main.ts`) later turns
each completed receipt into memory candidates via `/internal/memory/enqueue`.

Delivery rebuilds the identity **per row, from durable rows only**:

```text
receipt.session_id  → conversation_session.account_id
receipt.(session, request) → memory_source_event.business_user_id + event_id
```

Nothing on that path reads Node process state, which is why delivery still
happens after an idle eviction, a restart, or a `kill -9` between the receipt
completing and the enqueue. A delivery failure is counted and retried, and is
parked as `failed` after the attempt threshold; it never replays a model turn
or a tool call.

## Provider modes

`SMARTCS_PROVIDER_MODE` is explicit:

| value | behaviour |
|---|---|
| `faux` | offline provider. Always allowed — this is how a developer asks for it |
| `openai` | requires `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME`; refuses to start without them |
| unset | a complete model config ⇒ `openai`; otherwise `faux` **only inside a test process**, and a startup refusal anywhere else |

The refusal is the point: a served deployment with missing credentials used to
start happily and answer every user from the Faux provider — `/health` green,
product broken, nothing said so. A missing credential is a configuration error
and surfaces at startup.

Tests pass `provider: "faux"` explicitly (or set the env var), so they never
depend on the inference branch.

## Deployment boundary: single instance

`SessionRegistry` provides the same-session single-writer guarantee with an
**in-process** mutex. That is complete for one harness process and does not
extend across processes: two harness replicas share no lease, and the same
session could be written by both.

> **This harness runs as a single instance. Multi-replica operation is not
> supported and not claimed.**

If horizontal scaling is ever needed, the upgrade is a shared lease (MySQL or
Redis) plus session routing — deliberately not built pre-emptively.

## Tests

See [`tests/README.md`](tests/README.md) for the database-window discipline that
these suites require. Short version: they reset a real MySQL database, and heavy
suites must run one at a time.

The suite runs fully offline: anything that drives the agent loop uses the Faux
provider, and a test that reaches a real model endpoint is a bug.

Python-side acceptance for `internal_api` lives outside `python-impl/tests/`:

```bash
cd ../python-impl && python -m pytest ../pi-harness/tests/python/test_internal_api_auth.py -q
```

## Historical Migration Record

`PHASE0_REPORT.md` … `PHASE9_REPORT.md` are the **historical** acceptance record
of the Python → Pi migration. They are kept verbatim, including the deviations
they reported at the time; later phases closed some of them. They describe how
the system got here, not how it works now — read this README and
`../python-impl/docs/architecture.md` for the current state.
