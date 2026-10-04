/**
 * `/ready` — the harness's readiness probe (Phase 11 §4).
 *
 * `/health` stays what it always was: "this Node process is alive". `/ready`
 * answers the different, more expensive question an orchestrator actually needs:
 *
 *     can this instance serve a user right now?
 *
 * What is checked, and why exactly this much:
 *
 *   mysql              one `SELECT 1`. The receipt/provenance store is where a
 *                      turn's idempotency and crash recovery live; without it a
 *                      request cannot be served safely.
 *   business_runtime   `GET /internal/ready` on the Python runtime. That
 *                      endpoint is itself cheap by contract (no LLM, no RAG, no
 *                      business tool) — readiness must never execute business
 *                      work.
 *   mcp                ONLY when `SMARTCS_KNOWLEDGE_TRANSPORT=mcp`. With the
 *                      default `http` transport the gateway is not part of the
 *                      serving path at all, so it must not be able to make a
 *                      healthy deployment look unhealthy (`enabled: false`).
 *
 * A memory backlog is NOT a readiness failure (§8). Memory is an asynchronous
 * capability: chat, orders, refunds and RAG keep working while the outbox
 * drains, so a backlog reports `ready: true, degraded: true` with a warning
 * instead of pulling the instance out of rotation.
 *
 * Nothing here returns a URL, a credential, a SQL fragment or a driver message.
 * A failing check is a boolean.
 */

import { knowledgeMcpToken, knowledgeMcpUrl } from "../agent/mcp/knowledge-mcp.js";
import type { MemoryOutboxStats } from "../db/receipts.js";

export type WarningCode = "memory_outbox_backlog" | "memory_outbox_failed";

export interface ReadinessReport {
  ready: boolean;
  degraded: boolean;
  warnings: WarningCode[];
  checks: {
    mysql: { ok: boolean };
    business_runtime: { ok: boolean };
    mcp: { enabled: boolean; ok: boolean };
  };
}

export interface ReadinessDeps {
  /** Cheapest proof the receipt store can reach MySQL. Throws when it cannot. */
  pingMysql: () => Promise<void>;
  /** True when the Business Runtime reports itself ready. Never throws. */
  checkBusinessRuntime: () => Promise<boolean>;
  /**
   * The configured knowledge transport. `mcp` is the only value that puts the
   * gateway on the serving path; anything else (the default `http`) skips it.
   */
  knowledgeTransport: string;
  /** Durable outbox snapshot, for the degraded signal only. */
  outboxStats: () => Promise<MemoryOutboxStats>;
  /** Bounded so a hung dependency cannot hang the probe forever. */
  timeoutMs?: number;
}

export type ReadinessProbe = () => Promise<ReadinessReport>;

/** Readiness must answer fast: an orchestrator is waiting on it. */
export const DEFAULT_READINESS_TIMEOUT_MS = 3_000;

function notReady(checks: ReadinessReport["checks"]): ReadinessReport {
  return { ready: false, degraded: false, warnings: [], checks };
}

/** Resolve the gateway's `/health`, derived from the configured MCP URL. */
function mcpHealthUrl(): string {
  const url = new URL(knowledgeMcpUrl());
  url.pathname = "/health";
  url.search = "";
  url.hash = "";
  return url.toString();
}

export function createReadinessProbe(deps: ReadinessDeps): ReadinessProbe {
  const timeoutMs = deps.timeoutMs ?? DEFAULT_READINESS_TIMEOUT_MS;

  return async function probe(): Promise<ReadinessReport> {
    const checks: ReadinessReport["checks"] = {
      mysql: { ok: false },
      business_runtime: { ok: false },
      mcp: { enabled: deps.knowledgeTransport === "mcp", ok: true },
    };

    // MySQL first: if it is down, everything downstream is unanswerable anyway,
    // and there is no point spending the other calls' time budget.
    try {
      await deps.pingMysql();
      checks.mysql.ok = true;
    } catch {
      return notReady(checks);
    }

    try {
      checks.business_runtime.ok = await withTimeout(deps.checkBusinessRuntime(), timeoutMs);
    } catch {
      checks.business_runtime.ok = false;
    }
    if (!checks.business_runtime.ok) return notReady(checks);

    if (checks.mcp.enabled) {
      checks.mcp.ok = await checkMcpGateway(timeoutMs);
      if (!checks.mcp.ok) return notReady(checks);
    }

    // Past this point the instance CAN serve. A backlog is a degradation, not an
    // outage — and a stats read that fails is not a readiness failure either,
    // since MySQL was already proven reachable above.
    const warnings: WarningCode[] = [];
    try {
      const stats = await withTimeout(deps.outboxStats(), timeoutMs);
      if (stats.pending > 0) warnings.push("memory_outbox_backlog");
      if (stats.failed > 0) warnings.push("memory_outbox_failed");
    } catch {
      /* observation only; never turns a serving instance unready */
    }

    return {
      ready: true,
      degraded: warnings.includes("memory_outbox_backlog"),
      warnings,
      checks,
    };
  };
}

/**
 * The gateway is reachable, armed and initialized.
 *
 * `Authorization` is sent because `/health` sits INSIDE the gateway's token
 * guard: a 200 therefore also proves the server-level credential is correct,
 * which a bare "port is open" check would not. The token is read per call (not
 * captured) so a rotated credential is picked up without a restart; a missing
 * or malformed configuration is simply "not ready".
 */
async function checkMcpGateway(timeoutMs: number): Promise<boolean> {
  let url: string;
  let token: string;
  try {
    url = mcpHealthUrl();
    token = knowledgeMcpToken();
  } catch {
    return false;
  }
  try {
    const response = await fetch(url, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(timeoutMs),
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function withTimeout<T>(work: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("readiness check timed out")), timeoutMs);
        timer.unref?.();
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
