/**
 * pi-harness HTTP surface (Phase 1).
 *
 * Public (browser-facing):
 *   POST /api/chat          JSON final
 *   POST /api/chat/stream   SSE frames: status* -> final -> done
 * Probes (no credential, orchestration surface):
 *   GET    /health          liveness  — "this process is alive"
 *   GET    /ready           readiness — "this instance can serve a user"
 * Internal (Python Business Runtime only, service JWT):
 *   GET    /internal/history/{session_id}
 *   DELETE /internal/history/{session_id}
 *   GET    /internal/ops/memory-outbox   (Phase 11: observe only)
 *
 * Phase 7 unified entry: the Business Runtime forwards a pi session's turns
 * here, carrying the caller's own user JWT verbatim plus a service signature
 * header. The user token remains the only identity source; the service header,
 * when present, must verify (see `verifyForwardedServiceToken`).
 *
 * Deliberately minimal: no CORS config, no rate limiting, no metrics — those
 * belong to later phases. The edge does not persist business state.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { existsSync, unlinkSync } from "node:fs";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import {
  RUNTIME_TOKEN_AUDIENCE,
  RUNTIME_TOKEN_ISSUER,
  SERVICE_JWT_MAX_TTL_SECONDS,
  serviceJwtSecret,
  type SmartCsPaths,
} from "../config/env.js";
import { JwtError, verifyHs256 } from "../business/jwt-hs256.js";
import { PythonInternalClient } from "../business/python-client.js";
import type { MemorySourceStore } from "../db/memory-source.js";
import type { ReceiptStore } from "../db/receipts.js";
import { SessionRegistry } from "../session/registry.js";
import { openOrCreatePiSession } from "../session/pi-session.js";
import { projectHistory } from "../history/projector.js";
import { formatSseFrame } from "../streaming/status.js";
import type { OutboxProcessStats } from "../session/outbox.js";
import { resolveKnowledgeTransport } from "../agent/mcp/knowledge-mcp.js";
import { HttpError } from "./http-error.js";
import { runChat, type ChatPipelineDeps } from "./chat-pipeline.js";
import { authenticateRequest, AuthError } from "./user-auth.js";
import { createReadinessProbe, type ReadinessProbe, type ReadinessReport } from "./readiness.js";

export interface HarnessServerDeps extends Omit<ChatPipelineDeps, "registry"> {
  paths: SmartCsPaths;
  /** Optional: defaults to the real 先查后建 Pi session factory. */
  registry?: SessionRegistry;
  /**
   * Optional: defaults to a probe over `receipts` (MySQL ping + outbox backlog)
   * and `pythonClient` (`/internal/ready`). Tests override it to drive the
   * failure paths without breaking a real dependency.
   */
  readiness?: ReadinessProbe;
  /**
   * Optional: the running dispatcher's process-local counters, surfaced by
   * `/internal/ops/memory-outbox`. Absent when no dispatcher was started (the
   * endpoint then reports the durable state only).
   */
  outboxStats?: () => OutboxProcessStats;
}

export interface HarnessServer {
  server: Server;
  registry: SessionRegistry;
  listen(port: number, host?: string): Promise<{ port: number }>;
  close(): Promise<void>;
}

const MAX_BODY_BYTES = 1_000_000;
/** Header the Business Runtime signs a forwarded turn with (Phase 7). */
const SERVICE_TOKEN_HEADER = "x-smartcs-service-token";

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = chunk as Buffer;
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, "request body too large");
    chunks.push(buffer);
  }
  if (!size) throw new HttpError(400, "empty request body");
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new HttpError(400, "invalid request body");
    }
    return parsed as Record<string, unknown>;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "invalid request body");
  }
}

function sendJson(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(body);
}

function sendError(res: ServerResponse, error: unknown): void {
  if (error instanceof HttpError) {
    sendJson(res, error.status, { detail: error.detail });
    return;
  }
  if (error instanceof AuthError) {
    sendJson(res, error.status, { detail: error.detail });
    return;
  }
  // Internals never reach the caller; SMARTCS_DEBUG_ERRORS is an explicitly
  // opt-in operator switch for local diagnosis only.
  if (process.env.SMARTCS_DEBUG_ERRORS === "1") {
    console.error("[harness] unhandled error:", error);
  }
  sendJson(res, 500, { detail: "internal error" });
}

/** Verify the Business Runtime's service token on an internal request. */
function requireServiceIdentity(req: IncomingMessage): { accountId: number; sessionId: string } {
  const header = req.headers.authorization;
  if (!header) throw new HttpError(401, "service authentication required");
  const parts = header.split(/\s+/);
  if (parts.length !== 2 || parts[0]?.toLowerCase() !== "bearer" || !parts[1]) {
    throw new HttpError(401, "invalid service authentication");
  }
  try {
    const claims = verifyHs256(parts[1], {
      secret: serviceJwtSecret(),
      issuer: RUNTIME_TOKEN_ISSUER,
      audience: RUNTIME_TOKEN_AUDIENCE,
      maxTtlSeconds: SERVICE_JWT_MAX_TTL_SECONDS,
      requiredClaims: ["account_id", "session_id", "client_request_id"],
    });
    if (typeof claims.account_id !== "number" || typeof claims.session_id !== "string") {
      throw new JwtError("invalid service claims");
    }
    return { accountId: claims.account_id, sessionId: claims.session_id };
  } catch {
    throw new HttpError(401, "invalid service authentication");
  }
}

/**
 * Verify a service token that carries no turn identity (Phase 11 ops surface).
 *
 * `/internal/ops/memory-outbox` belongs to no turn: it is the deployment's
 * operator (or its peer service) asking about durable state, so there is no
 * `account_id` / `session_id` to require — but it is emphatically not public.
 * Tolerating this shape at the two per-turn call sites would weaken them, so it
 * gets its own named verifier rather than a relaxed flag on theirs.
 */
function requireOpsServiceIdentity(req: IncomingMessage): void {
  const header = req.headers.authorization;
  if (!header) throw new HttpError(401, "service authentication required");
  const parts = header.split(/\s+/);
  if (parts.length !== 2 || parts[0]?.toLowerCase() !== "bearer" || !parts[1]) {
    throw new HttpError(401, "invalid service authentication");
  }
  try {
    verifyHs256(parts[1], {
      secret: serviceJwtSecret(),
      issuer: RUNTIME_TOKEN_ISSUER,
      audience: RUNTIME_TOKEN_AUDIENCE,
      maxTtlSeconds: SERVICE_JWT_MAX_TTL_SECONDS,
    });
  } catch {
    throw new HttpError(401, "invalid service authentication");
  }
}

/**
 * Phase 7: verify the forwarding runtime's signature when it is present.
 *
 * The runtime forwards a turn with `X-SmartCS-Service-Token: Bearer <jwt>`, the
 * same service credential every other runtime -> harness call uses. The header
 * is optional so the Phase 1-6 direct edge contract (user JWT only) is
 * unchanged; but a header that IS present must verify — a tampered or stale
 * signature is refused instead of being silently ignored.
 */
function verifyForwardedServiceToken(req: IncomingMessage): void {
  const header = req.headers[SERVICE_TOKEN_HEADER];
  if (header === undefined) return;
  const value = Array.isArray(header) ? header[0] : header;
  const parts = (value ?? "").split(/\s+/);
  if (parts.length !== 2 || parts[0]?.toLowerCase() !== "bearer" || !parts[1]) {
    throw new HttpError(401, "invalid service authentication");
  }
  try {
    verifyHs256(parts[1], {
      secret: serviceJwtSecret(),
      issuer: RUNTIME_TOKEN_ISSUER,
      audience: RUNTIME_TOKEN_AUDIENCE,
      maxTtlSeconds: SERVICE_JWT_MAX_TTL_SECONDS,
      requiredClaims: ["account_id", "session_id", "client_request_id"],
    });
  } catch {
    throw new HttpError(401, "invalid service authentication");
  }
}

export function createHarnessServer(deps: HarnessServerDeps): HarnessServer {
  const registry =
    deps.registry ??
    new SessionRegistry(
      async (sessionId) => {
        const handle = await openOrCreatePiSession(deps.paths, sessionId);
        return {
          session: handle.session,
          origin: handle.origin,
          turnContext: handle.turnContext,
          snapshotHolder: handle.snapshotHolder,
        };
      },
      {},
    );

  const pipelineDeps: ChatPipelineDeps = {
    registry,
    receipts: deps.receipts,
    memorySource: deps.memorySource,
    pythonClient: deps.pythonClient,
    idleEvictionMs: deps.idleEvictionMs,
    classifyIntent: deps.classifyIntent,
  };

  const readiness =
    deps.readiness ??
    createReadinessProbe({
      pingMysql: () => deps.receipts.ping(),
      checkBusinessRuntime: () => deps.pythonClient.checkReady(),
      // Resolved once, here: a malformed switch must fail at construction, not
      // silently decide that the gateway is out of scope for readiness.
      knowledgeTransport: resolveKnowledgeTransport(),
      outboxStats: () => deps.receipts.memoryOutboxStats(),
    });

  const server = createServer((req, res) => {
    void handle(req, res).catch((error) => {
      if (!res.headersSent) sendError(res, error);
      else res.end();
    });
  });

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://localhost");
    const path = url.pathname;

    if (path === "/health" && req.method === "GET") {
      sendJson(res, 200, { ok: true, sessions: registry.stats() });
      return;
    }

    if (path === "/ready" && req.method === "GET") {
      await handleReady(res);
      return;
    }

    if ((path === "/api/chat" || path === "/api/chat/stream") && req.method === "POST") {
      await handleChat(req, res, path.endsWith("/stream"));
      return;
    }

    if (path === "/internal/ops/memory-outbox" && req.method === "GET") {
      requireOpsServiceIdentity(req);
      await handleMemoryOutboxOps(res);
      return;
    }

    const internal = /^\/internal\/history\/([^/]+)$/.exec(path);
    if (internal) {
      requireServiceIdentity(req);
      const sessionId = decodeURIComponent(internal[1]!);
      if (req.method === "GET") {
        await handleHistoryGet(res, sessionId);
        return;
      }
      if (req.method === "DELETE") {
        await handleHistoryDelete(res, sessionId);
        return;
      }
    }

    sendJson(res, 404, { detail: "not found" });
  }

  /**
   * `/ready` — the probe that says "this instance can serve a user".
   *
   * Unauthenticated like `/health`: an orchestrator's healthcheck carries no
   * credential, and the body is booleans plus a warning code — no URL, no
   * secret, no driver text. A probe that throws is a programming error, not a
   * dependency outage, but the honest answer is still "nothing is proven, so
   * not ready" rather than a 500 an orchestrator would read as a crash-loop.
   */
  async function handleReady(res: ServerResponse): Promise<void> {
    let report: ReadinessReport;
    try {
      report = await readiness();
    } catch {
      report = {
        ready: false,
        degraded: false,
        warnings: [],
        checks: { mysql: { ok: false }, business_runtime: { ok: false }, mcp: { enabled: false, ok: false } },
      };
    }
    sendJson(res, report.ready ? 200 : 503, report);
  }

  /**
   * `/internal/ops/memory-outbox` — observe only (plan §7).
   *
   * Returns the durable aggregate (authoritative) and this process's dispatcher
   * counters (observation). It changes no receipt, retries nothing and never
   * reaches the Business Runtime: the manual replay lives in the CLI and its
   * only effect is moving one row back to `pending` for the normal dispatcher.
   */
  async function handleMemoryOutboxOps(res: ServerResponse): Promise<void> {
    let durable;
    try {
      durable = await deps.receipts.memoryOutboxStats();
    } catch {
      // An unreachable store is the one thing an operator must learn here; the
      // reason stays in the runtime's logs.
      sendJson(res, 503, { detail: "outbox state unavailable" });
      return;
    }
    // Wire format is snake_case throughout, matching the `durable` block and the
    // internal channel's conventions; the TypeScript types stay camelCase.
    const dispatcher = deps.outboxStats?.();
    sendJson(res, 200, {
      durable: {
        pending: durable.pending,
        failed: durable.failed,
        oldest_pending_age_seconds: durable.oldestPendingAgeSeconds,
        max_attempts: durable.maxPendingAttempts,
      },
      dispatcher: dispatcher
        ? {
            passes: dispatcher.passes,
            delivered: dispatcher.delivered,
            delivery_failures: dispatcher.deliveryFailures,
            parked: dispatcher.parked,
            last_dispatch_at: dispatcher.lastDispatchAt,
            last_success_at: dispatcher.lastSuccessAt,
            last_error_at: dispatcher.lastErrorAt,
          }
        : null,
    });
  }

  async function handleChat(req: IncomingMessage, res: ServerResponse, stream: boolean): Promise<void> {
    // (0) Phase 7: a forwarded turn carries the runtime's signature as well.
    verifyForwardedServiceToken(req);
    // (1) local token verification
    const claims = authenticateRequest({
      authorization: req.headers.authorization,
      cookie: req.headers.cookie,
    });
    const body = await readJsonBody(req);

    // Shape validation happens inside runChat (it must re-check anyway, since
    // these values reach SQL parameters and the Pi prompt).
    const input = {
      userJwt: claims.token,
      accountId: claims.accountId,
      sessionId: body.session_id as string,
      clientRequestId: body.client_request_id as string,
      message: body.message as string,
      // Phase 6: an upstream gateway may start the trace; a malformed value is
      // simply ignored (the harness mints its own).
      traceparent: typeof req.headers.traceparent === "string" ? req.headers.traceparent : undefined,
    };

    if (!stream) {
      const result = await runChat(pipelineDeps, input);
      sendJson(res, 200, {
        session_id: result.sessionId,
        client_request_id: result.clientRequestId,
        message: result.message,
        replayed: result.replayed,
        // Phase 7: the observation-only label the turn was stored with, for the
        // Business Runtime to surface as `intent` on the unified entry. It
        // decides nothing here and nothing upstream routes on it.
        intent_label: result.meta.intentLabel,
      });
      return;
    }

    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-store, no-transform",
      Connection: "keep-alive",
    });

    let aborted = false;
    req.on("close", () => {
      aborted = true;
    });
    // A client that disappears mid-stream makes every later write fail on a
    // dead socket. That is a normal disconnect, not a server fault: without a
    // listener the stream error would be rethrown and take the process with it
    // (F9: the run must survive the viewer leaving, and the ledger — not the
    // socket — decides what happened to the write).
    res.on("error", () => {
      aborted = true;
    });

    try {
      const result = await runChat(pipelineDeps, {
        ...input,
        onFrame: (frame) => {
          if (!aborted) res.write(formatSseFrame(frame));
        },
      });
      if (!aborted) {
        res.write(
          `event: meta\ndata: ${JSON.stringify({
            session_id: result.sessionId,
            client_request_id: result.clientRequestId,
            replayed: result.replayed,
          })}\n\n`,
        );
      }
    } catch (error) {
      if (!aborted) {
        const status = error instanceof HttpError ? error.status : error instanceof AuthError ? error.status : 500;
        const detail =
          error instanceof HttpError ? error.detail : error instanceof AuthError ? error.detail : "internal error";
        res.write(`event: error\ndata: ${JSON.stringify({ status, detail })}\n\n`);
        res.write(formatSseFrame({ type: "done", at: Date.now(), reason: "error" }));
      }
    } finally {
      res.end();
    }
  }

  async function handleHistoryGet(res: ServerResponse, sessionId: string): Promise<void> {
    const manager = openExistingManager(sessionId);
    if (!manager) {
      sendJson(res, 404, { detail: "session not found" });
      return;
    }
    sendJson(res, 200, { session_id: sessionId, messages: projectHistory(manager.getEntries()) });
  }

  async function handleHistoryDelete(res: ServerResponse, sessionId: string): Promise<void> {
    // Refuse while a run is active rather than queueing behind it.
    const lease = registry.tryAcquire(sessionId);
    if (!lease) {
      sendJson(res, 409, { detail: "unfinished request cannot be deleted" });
      return;
    }
    try {
      const manager = registry.get(sessionId)?.session.sessionManager ?? openExistingManager(sessionId);
      const file = manager?.getSessionFile();
      registry.disposeNow(sessionId);
      if (file && existsSync(file)) unlinkSync(file);
      // Provenance rows are retained for audit but marked as cleared.
      const cleared = await deps.memorySource.markCleared(sessionId);
      sendJson(res, 200, { session_id: sessionId, deleted: true, cleared_source_events: cleared });
    } finally {
      lease.release();
    }
  }

  /** Open an existing transcript for read/delete; never create one implicitly. */
  function openExistingManager(sessionId: string): SessionManager | undefined {
    const live = registry.get(sessionId)?.session.sessionManager;
    if (live) return live;
    const found = SessionManager.findById(deps.paths.runtimeCwd, sessionId, deps.paths.sessionDir);
    if (!found || !existsSync(found)) return undefined;
    const manager = SessionManager.open(found, deps.paths.sessionDir, deps.paths.runtimeCwd);
    return manager.getSessionId() === sessionId ? manager : undefined;
  }

  return {
    server,
    registry,
    listen(port: number, host = "127.0.0.1") {
      return new Promise((resolve) => {
        server.listen(port, host, () => {
          const address = server.address();
          resolve({ port: typeof address === "object" && address ? address.port : port });
        });
      });
    },
    close() {
      return new Promise((resolve, reject) => {
        void registry.shutdown().then(() => {
          // Sessions first, then transport. Idle keep-alive sockets would
          // otherwise hold `close()` open indefinitely; remaining in-flight
          // streams are dropped, which is acceptable for a controlled Phase 1
          // shutdown (no graceful drain requirement yet).
          server.closeIdleConnections?.();
          server.close((error) => (error ? reject(error) : resolve()));
          server.closeAllConnections?.();
        });
      });
    },
  };
}

export { PythonInternalClient };
