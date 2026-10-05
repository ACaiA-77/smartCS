/**
 * Client for the Python Business Runtime internal API.
 *
 * Phase 1 calls exactly one endpoint: identity resolution. The harness asserts
 * nothing about who the user is — it forwards the *raw user token* and lets
 * Python decide, which keeps `account -> business_user` single-authoritative
 * (plan v2 §6.1).
 */

import {
  SERVICE_JWT_MAX_TTL_SECONDS,
  SERVICE_TOKEN_AUDIENCE,
  SERVICE_TOKEN_ISSUER,
  pythonInternalBaseUrl,
  serviceJwtSecret,
} from "../config/env.js";
import { signHs256 } from "./jwt-hs256.js";
import type { TurnIdentity } from "./turn-context.js";

export interface ServiceTokenInput {
  accountId: number;
  /** Omitted for /internal/auth/verify, which is what resolves it. */
  businessUserId?: string;
  sessionId: string;
  clientRequestId: string;
}

export function mintServiceToken(input: ServiceTokenInput, ttlSeconds = SERVICE_JWT_MAX_TTL_SECONDS): string {
  if (!Number.isInteger(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > SERVICE_JWT_MAX_TTL_SECONDS) {
    throw new Error("service token ttl out of range");
  }
  const now = Math.floor(Date.now() / 1000);
  const claims: Record<string, unknown> = {
    iss: SERVICE_TOKEN_ISSUER,
    aud: SERVICE_TOKEN_AUDIENCE,
    account_id: input.accountId,
    session_id: input.sessionId,
    client_request_id: input.clientRequestId,
    iat: now,
    exp: now + ttlSeconds,
  };
  if (input.businessUserId !== undefined) claims.business_user_id = input.businessUserId;
  return signHs256(claims, serviceJwtSecret());
}

/**
 * A service token for a call that belongs to NO turn (Phase 11 readiness).
 *
 * Same secret, issuer, audience and TTL as every other internal call — the
 * caller is still provably the peer service — but with no `account_id` /
 * `session_id` / `client_request_id`, because there is no turn to bind. The
 * runtime verifies it with `decode_ops_service_token`, which requires exactly
 * these claims and no more.
 */
export function mintOpsServiceToken(ttlSeconds = SERVICE_JWT_MAX_TTL_SECONDS): string {
  if (!Number.isInteger(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > SERVICE_JWT_MAX_TTL_SECONDS) {
    throw new Error("service token ttl out of range");
  }
  const now = Math.floor(Date.now() / 1000);
  return signHs256(
    {
      iss: SERVICE_TOKEN_ISSUER,
      aud: SERVICE_TOKEN_AUDIENCE,
      iat: now,
      exp: now + ttlSeconds,
    },
    serviceJwtSecret(),
  );
}

export interface VerifiedIdentity {
  account_id: number;
  business_user_id: string;
  status: string;
  session_id: string;
  harness_version: string;
}

/** Carries the upstream HTTP status so the edge can mirror it faithfully. */
export class PythonInternalError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`python internal error ${status}: ${detail}`);
  }
}

export interface PythonClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /**
   * Timeout for `/internal/tools/execute` only. A tool call runs business work
   * — a retrieval pass, a query — whose latency belongs to the deployment, not
   * to the protocol; the 10s transport default was measured below the real RAG
   * latency (9-14s), which turned a healthy tool into an intermittent 504.
   * Every other internal call stays on `timeoutMs`.
   */
  toolTimeoutMs?: number;
}

/** Deployment tuning, not a business rule: set it to the real retrieval latency. */
export const TOOL_TIMEOUT_ENV = "SMARTCS_TOOL_TIMEOUT_MS";
export const DEFAULT_TOOL_TIMEOUT_MS = 30_000;

function resolveToolTimeoutMs(): number {
  const raw = process.env[TOOL_TIMEOUT_ENV];
  if (raw === undefined || raw.trim() === "") return DEFAULT_TOOL_TIMEOUT_MS;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`invalid ${TOOL_TIMEOUT_ENV}: ${raw} (expected milliseconds)`);
  }
  return value;
}

/**
 * Phase 6 propagation headers (plan v2 §6.9). `traceparent` is W3C-standard;
 * the id headers are harness-specific and only ever ADD attributes to the
 * Python span — the token remains the only authoritative identity source.
 */
export function traceHeaders(identity: TurnIdentity): Record<string, string> {
  const headers: Record<string, string> = {};
  if (identity.traceparent) headers.traceparent = identity.traceparent;
  if (identity.agentRunId) headers["x-smartcs-agent-run-id"] = identity.agentRunId;
  return headers;
}

/**
 * One audit row as the Business Runtime expects it (phase6-design.md §3).
 *
 * The column set is exactly the design's: `occurred_at` is not a column, so
 * the dispatcher folds the harness-side timestamp into `payload`.
 */
export interface AuditEventPayload {
  event_id: string;
  kind: "tool_call" | "tool_result";
  tool_name: string;
  tool_call_id: string | null;
  operation_id: string | null;
  trace_id: string | null;
  payload: unknown;
}

export class PythonInternalClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly toolTimeoutMs: number;

  constructor(options: PythonClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? pythonInternalBaseUrl()).replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.toolTimeoutMs = options.toolTimeoutMs ?? resolveToolTimeoutMs();
  }

  /** POST /internal/auth/verify — the only Phase 1 internal call. */
  async verifyIdentity(params: {
    userJwt: string;
    sessionId: string;
    accountId: number;
    clientRequestId: string;
    /** Phase 6: this call happens before any receipt exists, so the caller
     *  passes the turn's context explicitly. */
    traceparent?: string;
  }): Promise<VerifiedIdentity> {
    const token = mintServiceToken({
      accountId: params.accountId,
      sessionId: params.sessionId,
      clientRequestId: params.clientRequestId,
    });

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/internal/auth/verify`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          ...(params.traceparent ? { traceparent: params.traceparent } : {}),
        },
        body: JSON.stringify({ user_jwt: params.userJwt, session_id: params.sessionId }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      // Network failure is "business runtime unavailable", never "denied".
      throw new PythonInternalError(503, "business runtime unreachable");
    }

    if (!response.ok) {
      let detail = "internal request rejected";
      try {
        const body = (await response.json()) as { detail?: string };
        if (typeof body?.detail === "string") detail = body.detail;
      } catch {
        /* keep the generic detail */
      }
      throw new PythonInternalError(response.status, detail);
    }

    const body = (await response.json()) as Partial<VerifiedIdentity>;
    if (
      typeof body.account_id !== "number" ||
      typeof body.business_user_id !== "string" ||
      typeof body.session_id !== "string" ||
      typeof body.harness_version !== "string"
    ) {
      throw new PythonInternalError(502, "malformed identity response");
    }
    return {
      account_id: body.account_id,
      business_user_id: body.business_user_id,
      status: String(body.status ?? ""),
      session_id: body.session_id,
      harness_version: body.harness_version,
    };
  }

  /**
   * POST /internal/tools/execute — one READ tool call.
   *
   * The service token carries the resolved `business_user_id` (required from
   * Phase 2 on). Nothing here retries or interprets the business answer: the
   * runtime owns retry/timeout/authorization, the shell is transport only.
   */
  async executeTool(params: {
    tool: string;
    arguments: Record<string, unknown>;
    identity: TurnIdentity;
    /** Pi's id for this call — recorded on the Python span (Phase 6). */
    toolCallId?: string;
    signal?: AbortSignal;
  }): Promise<{ ok: boolean; content: string; details: unknown }> {
    const token = mintServiceToken({
      accountId: params.identity.accountId,
      businessUserId: params.identity.businessUserId,
      sessionId: params.identity.sessionId,
      clientRequestId: params.identity.clientRequestId,
    });

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/internal/tools/execute`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          ...traceHeaders(params.identity),
          ...(params.toolCallId ? { "x-smartcs-tool-call-id": params.toolCallId } : {}),
        },
        body: JSON.stringify({
          tool: params.tool,
          arguments: params.arguments,
          session_id: params.identity.sessionId,
          client_request_id: params.identity.clientRequestId,
        }),
        signal: params.signal
          ? AbortSignal.any([params.signal, AbortSignal.timeout(this.toolTimeoutMs)])
          : AbortSignal.timeout(this.toolTimeoutMs),
      });
    } catch (error) {
      if (params.signal?.aborted) throw new PythonInternalError(499, "tool call aborted");
      if (error instanceof Error && error.name === "TimeoutError") {
        throw new PythonInternalError(504, "tool call timed out");
      }
      throw new PythonInternalError(503, "business runtime unreachable");
    }

    if (!response.ok) {
      let code = "tool_call_rejected";
      let message = "internal tool request rejected";
      try {
        const body = (await response.json()) as { detail?: unknown };
        const detail = body?.detail;
        if (typeof detail === "string") message = detail;
        else if (detail && typeof detail === "object") {
          const shaped = detail as { code?: unknown; message?: unknown };
          if (typeof shaped.code === "string") code = shaped.code;
          if (typeof shaped.message === "string") message = shaped.message;
        }
      } catch {
        /* keep the generic wording */
      }
      throw new PythonInternalError(response.status, `${code}: ${message}`);
    }

    const body = (await response.json()) as { ok?: unknown; content?: unknown; details?: unknown };
    if (typeof body.content !== "string") {
      throw new PythonInternalError(502, "malformed tool response");
    }
    return { ok: body.ok === true, content: body.content, details: body.details ?? null };
  }

  /**
   * GET /internal/ready — is the Business Runtime able to serve a turn?
   *
   * Deliberately separate from `verifyIdentity`: readiness runs on a timer and
   * must be cheap, so this asks the runtime's own dependency probe rather than
   * exercising a real request. A non-2xx answer, a malformed body, a timeout or
   * a connection refusal all mean the same thing to the caller — "not ready" —
   * so the reason stays in the runtime's logs and never reaches the probe
   * response.
   */
  async checkReady(signal?: AbortSignal): Promise<boolean> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/internal/ready`, {
        method: "GET",
        headers: { Authorization: `Bearer ${mintOpsServiceToken()}` },
        signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(this.timeoutMs)])
          : AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      return false;
    }
    if (!response.ok) return false;
    try {
      const body = (await response.json()) as { ok?: unknown };
      return body.ok === true;
    } catch {
      return false;
    }
  }

  /** Shared signed POST used by the Phase 3+ endpoints (and Phase 6 audit). */
  private async postInternal<T>(
    path: string,
    identity: TurnIdentity,
    payload: Record<string, unknown>,
    signal?: AbortSignal,
    extraHeaders: Record<string, string> = {},
  ): Promise<T> {
    const token = mintServiceToken({
      accountId: identity.accountId,
      businessUserId: identity.businessUserId,
      sessionId: identity.sessionId,
      clientRequestId: identity.clientRequestId,
    });
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          ...traceHeaders(identity),
          ...extraHeaders,
        },
        body: JSON.stringify(payload),
        signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(this.timeoutMs)])
          : AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      if (signal?.aborted) throw new PythonInternalError(499, "request aborted");
      if (error instanceof Error && error.name === "TimeoutError") {
        throw new PythonInternalError(504, "request timed out");
      }
      throw new PythonInternalError(503, "business runtime unreachable");
    }
    if (!response.ok) {
      let detail = "internal request rejected";
      try {
        const body = (await response.json()) as { detail?: unknown };
        detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? detail);
      } catch {
        /* keep the generic wording */
      }
      throw new PythonInternalError(response.status, detail);
    }
    return (await response.json()) as T;
  }

  /** POST /internal/context/turn-snapshot — prefetched BEFORE the model runs. */
  async fetchTurnSnapshot(params: {
    identity: TurnIdentity;
    signal?: AbortSignal;
  }): Promise<TurnSnapshot> {
    const body = await this.postInternal<{
      blocks?: Array<{ kind?: unknown; title?: unknown; content?: unknown; priority?: unknown }>;
      tokenBudget?: unknown;
    }>(
      "/internal/context/turn-snapshot",
      params.identity,
      { session_id: params.identity.sessionId, client_request_id: params.identity.clientRequestId },
      params.signal,
    );
    const blocks = (body.blocks ?? []).map((block) => ({
      kind: String(block.kind ?? "unknown"),
      title: String(block.title ?? ""),
      content: String(block.content ?? ""),
      priority: Number(block.priority ?? 100),
    }));
    return { blocks, tokenBudget: Number(body.tokenBudget ?? 0) };
  }

  /** POST /internal/memory/enqueue — outbox dispatcher call. */
  async enqueueMemory(params: {
    identity: TurnIdentity;
    sourceEventId: string;
    signal?: AbortSignal;
  }): Promise<{ enqueued: boolean; result: unknown }> {
    const body = await this.postInternal<{ enqueued?: unknown; result?: unknown }>(
      "/internal/memory/enqueue",
      params.identity,
      {
        session_id: params.identity.sessionId,
        client_request_id: params.identity.clientRequestId,
        source_event_id: params.sourceEventId,
      },
      params.signal,
    );
    return { enqueued: body.enqueued !== false, result: body.result ?? null };
  }

  /**
   * POST /internal/operation_status — the AUTHORITATIVE outcome of a write.
   *
   * Called only when the harness cannot tell from the response whether a side
   * effect happened. The ledger is the system of record; an HTTP error is not.
   */
  async operationStatus(params: {
    identity: TurnIdentity;
    operationId: string;
    signal?: AbortSignal;
  }): Promise<{ status: "COMPLETED" | "FAILED" | "PROVABLY_NOT_EXECUTED" | "UNKNOWN"; detail: string; result: unknown }> {
    const body = await this.postInternal<{ status?: unknown; detail?: unknown; result?: unknown }>(
      "/internal/operation_status",
      params.identity,
      {
        session_id: params.identity.sessionId,
        client_request_id: params.identity.clientRequestId,
        operation_id: params.operationId,
      },
      params.signal,
      // The body carries it for the handler; the header lets the runtime's span
      // label the request without parsing the body (Phase 6).
      { "x-smartcs-operation-id": params.operationId },
    );
    const status = String(body.status ?? "UNKNOWN");
    if (!["COMPLETED", "FAILED", "PROVABLY_NOT_EXECUTED", "UNKNOWN"].includes(status)) {
      // An unrecognised verdict is treated as UNKNOWN by construction.
      return { status: "UNKNOWN", detail: `unrecognised status: ${status}`, result: null };
    }
    return {
      status: status as "COMPLETED" | "FAILED" | "PROVABLY_NOT_EXECUTED" | "UNKNOWN",
      detail: String(body.detail ?? ""),
      result: body.result ?? null,
    };
  }

  /**
   * POST /internal/audit — best-effort batch delivery of audit records.
   *
   * Unlike every other method here this one is called from a BACKGROUND
   * dispatcher, never from a pi hook; a rejection is counted and dropped (the
   * audit is explicitly allowed to lose its tail — see the dispatcher).
   */
  async postAudit(params: {
    identity: TurnIdentity;
    events: AuditEventPayload[];
    signal?: AbortSignal;
  }): Promise<{ inserted: number; duplicates: number }> {
    const body = await this.postInternal<{ inserted?: unknown; duplicates?: unknown }>(
      "/internal/audit",
      params.identity,
      {
        session_id: params.identity.sessionId,
        client_request_id: params.identity.clientRequestId,
        events: params.events,
      },
      params.signal,
    );
    return { inserted: Number(body.inserted ?? 0), duplicates: Number(body.duplicates ?? 0) };
  }

  /** POST /internal/compliance/review — rule mask (always) + optional LLM stage. */
  async reviewCompliance(params: {
    identity: TurnIdentity;
    text: string;
    intentLabel?: string;
    signal?: AbortSignal;
  }): Promise<ComplianceVerdict> {
    const body = await this.postInternal<{
      verdict?: unknown;
      replacement?: unknown;
      rulesHit?: unknown;
      llmReviewed?: unknown;
    }>(
      "/internal/compliance/review",
      params.identity,
      {
        text: params.text,
        session_id: params.identity.sessionId,
        client_request_id: params.identity.clientRequestId,
        intent_label: params.intentLabel,
      },
      params.signal,
    );
    const verdict = String(body.verdict ?? "pass");
    if (verdict !== "pass" && verdict !== "sanitize" && verdict !== "fail") {
      throw new PythonInternalError(502, "unknown compliance verdict");
    }
    return {
      verdict,
      replacement: typeof body.replacement === "string" ? body.replacement : null,
      rulesHit: Array.isArray(body.rulesHit) ? body.rulesHit.map(String) : [],
      llmReviewed: body.llmReviewed === true,
    };
  }
}

export interface TurnSnapshotBlock {
  kind: string;
  title: string;
  content: string;
  priority: number;
}

export interface TurnSnapshot {
  blocks: TurnSnapshotBlock[];
  tokenBudget: number;
}

export interface ComplianceVerdict {
  verdict: "pass" | "sanitize" | "fail";
  replacement: string | null;
  rulesHit: string[];
  llmReviewed: boolean;
}
