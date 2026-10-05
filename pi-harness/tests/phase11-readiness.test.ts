/**
 * Phase 11 §4/§5/§8 — the readiness probe.
 *
 * `/health` answers "the process is alive"; `/ready` answers "this instance can
 * serve a user", and the difference is the whole point. These cases pin:
 *
 *   P11-R1  healthy dependencies                        → 200 ready
 *   P11-R2  MySQL unreachable                           → 503
 *   P11-R3  Business Runtime unreachable                → 503
 *   P11-R4  transport=http never touches the MCP gateway → stays ready with it down
 *   P11-R5  transport=mcp + gateway down                → 503
 *   P11-R6  transport=mcp + gateway up                  → 200
 *   P11-R7  a memory backlog is DEGRADED, not DOWN      → 200 degraded
 *   P11-R8  parked rows warn without degrading
 *   P11-R9  a hung dependency is bounded by the probe timeout
 *   P11-R10 the HTTP surface: /health stays light, /ready mirrors the probe,
 *           and neither body leaks a URL, a secret or driver text
 *
 * FULLY OFFLINE: every dependency is injected (the probe takes functions, not
 * connections), and the one HTTP server in here is a local stub standing in
 * for the MCP gateway. No MySQL, no Python, no model.
 */

import { createServer, type Server } from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { createHarnessServer } from "../src/server/app.js";
import { createReadinessProbe, type ReadinessDeps } from "../src/server/readiness.js";
import type { ReceiptStore } from "../src/db/receipts.js";
import type { MemorySourceStore } from "../src/db/memory-source.js";
import type { PythonInternalClient } from "../src/business/python-client.js";
import { resolveSmartCsPaths } from "../src/config/env.js";

const ZERO_STATS = { pending: 0, failed: 0, oldestPendingAgeSeconds: 0, maxPendingAttempts: 0 };

function deps(overrides: Partial<ReadinessDeps> = {}): ReadinessDeps {
  return {
    pingMysql: async () => undefined,
    checkBusinessRuntime: async () => true,
    knowledgeTransport: "http",
    outboxStats: async () => ({ ...ZERO_STATS }),
    timeoutMs: 500,
    ...overrides,
  };
}

const openServers: Server[] = [];

afterEach(async () => {
  await Promise.all(
    openServers.splice(0).map(
      (server) => new Promise<void>((resolve) => server.close(() => resolve())),
    ),
  );
});

/** A stand-in for the MCP gateway: `/health` behind the same bearer guard. */
async function startFakeGateway(expectedToken: string): Promise<string> {
  const server = createServer((req, res) => {
    if (req.url?.split("?")[0] !== "/health" || req.method !== "GET") {
      res.writeHead(404).end();
      return;
    }
    if (req.headers.authorization !== `Bearer ${expectedToken}`) {
      res.writeHead(401, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "invalid MCP service token" }));
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  openServers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return `http://127.0.0.1:${port}`;
}

/** Point the MCP configuration at a gateway (or at nothing) for one test. */
function withMcpEnv(url: string | undefined, token: string | undefined): () => void {
  const saved = {
    url: process.env.SMARTCS_MCP_URL,
    token: process.env.SMARTCS_MCP_TOKEN,
  };
  if (url === undefined) delete process.env.SMARTCS_MCP_URL;
  else process.env.SMARTCS_MCP_URL = url;
  if (token === undefined) delete process.env.SMARTCS_MCP_TOKEN;
  else process.env.SMARTCS_MCP_TOKEN = token;
  return () => {
    if (saved.url === undefined) delete process.env.SMARTCS_MCP_URL;
    else process.env.SMARTCS_MCP_URL = saved.url;
    if (saved.token === undefined) delete process.env.SMARTCS_MCP_TOKEN;
    else process.env.SMARTCS_MCP_TOKEN = saved.token;
  };
}

describe("Phase 11 — readiness probe", () => {
  it("P11-R1: every dependency healthy → ready, not degraded, mcp out of scope", async () => {
    const report = await createReadinessProbe(deps())();
    expect(report.ready).toBe(true);
    expect(report.degraded).toBe(false);
    expect(report.warnings).toEqual([]);
    expect(report.checks).toEqual({
      mysql: { ok: true },
      business_runtime: { ok: true },
      // http transport: the gateway is not on the serving path, so it must not
      // be able to make a healthy deployment look unhealthy.
      mcp: { enabled: false, ok: true },
    });
  });

  it("P11-R2: MySQL unreachable → 503, and the other checks are not claimed", async () => {
    const report = await createReadinessProbe(
      deps({
        pingMysql: async () => {
          throw new Error("connect ECONNREFUSED 127.0.0.1:3307");
        },
      }),
    )();
    expect(report.ready).toBe(false);
    expect(report.checks.mysql.ok).toBe(false);
    expect(JSON.stringify(report)).not.toContain("ECONNREFUSED");
  });

  it("P11-R3: Business Runtime unreachable → 503", async () => {
    const report = await createReadinessProbe(deps({ checkBusinessRuntime: async () => false }))();
    expect(report.ready).toBe(false);
    expect(report.checks.mysql.ok).toBe(true);
    expect(report.checks.business_runtime.ok).toBe(false);
  });

  it("P11-R4: transport=http ignores the MCP gateway entirely", async () => {
    // No MCP configuration at all: with `http` this must still be ready, which
    // is exactly what would break if the check were unconditional.
    const restore = withMcpEnv(undefined, undefined);
    try {
      const report = await createReadinessProbe(deps({ knowledgeTransport: "http" }))();
      expect(report.ready).toBe(true);
      expect(report.checks.mcp).toEqual({ enabled: false, ok: true });
    } finally {
      restore();
    }
  });

  it("P11-R5: transport=mcp with the gateway down → 503", async () => {
    // A port nobody listens on: the check must fail closed, not throw.
    const restore = withMcpEnv("http://127.0.0.1:1/mcp", "phase11-mcp-token-0123456789");
    try {
      const report = await createReadinessProbe(deps({ knowledgeTransport: "mcp" }))();
      expect(report.ready).toBe(false);
      expect(report.checks.mcp).toEqual({ enabled: true, ok: false });
    } finally {
      restore();
    }
  });

  it("P11-R6: transport=mcp with the gateway up (and the token right) → ready", async () => {
    const token = "phase11-mcp-token-0123456789";
    const origin = await startFakeGateway(token);
    const restore = withMcpEnv(`${origin}/mcp`, token);
    try {
      const report = await createReadinessProbe(deps({ knowledgeTransport: "mcp" }))();
      expect(report.ready).toBe(true);
      expect(report.checks.mcp).toEqual({ enabled: true, ok: true });
    } finally {
      restore();
    }
  });

  it("P11-R6b: transport=mcp with a WRONG token is not ready", async () => {
    // Proves the check actually authenticates rather than just probing a port.
    const origin = await startFakeGateway("the-real-gateway-token-0123456789");
    const restore = withMcpEnv(`${origin}/mcp`, "a-different-token-0123456789");
    try {
      const report = await createReadinessProbe(deps({ knowledgeTransport: "mcp" }))();
      expect(report.ready).toBe(false);
      expect(report.checks.mcp.ok).toBe(false);
    } finally {
      restore();
    }
  });

  it("P11-R7: a memory backlog degrades but does NOT take the instance out of rotation", async () => {
    const report = await createReadinessProbe(
      deps({
        outboxStats: async () => ({ pending: 10, failed: 0, oldestPendingAgeSeconds: 63, maxPendingAttempts: 2 }),
      }),
    )();
    expect(report.ready).toBe(true);
    expect(report.degraded).toBe(true);
    expect(report.warnings).toEqual(["memory_outbox_backlog"]);
  });

  it("P11-R8: parked rows warn without degrading", async () => {
    const report = await createReadinessProbe(
      deps({ outboxStats: async () => ({ pending: 0, failed: 3, oldestPendingAgeSeconds: 0, maxPendingAttempts: 0 }) }),
    )();
    expect(report.ready).toBe(true);
    expect(report.degraded).toBe(false);
    expect(report.warnings).toEqual(["memory_outbox_failed"]);
  });

  it("P11-R8b: a failing stats read never turns a serving instance unready", async () => {
    const report = await createReadinessProbe(
      deps({
        outboxStats: async () => {
          throw new Error("stats unavailable");
        },
      }),
    )();
    expect(report.ready).toBe(true);
    expect(report.warnings).toEqual([]);
  });

  it("P11-R9: a hung dependency is bounded by the probe timeout", async () => {
    const started = Date.now();
    const report = await createReadinessProbe(
      deps({
        // Never settles: the probe must give up rather than hang the caller.
        checkBusinessRuntime: () => new Promise<boolean>(() => undefined),
        timeoutMs: 100,
      }),
    )();
    expect(report.ready).toBe(false);
    expect(Date.now() - started).toBeLessThan(2_000);
  });
});

describe("Phase 11 — the HTTP surface", () => {
  /** A server exercising ONLY the probe routes; no chat path is reached. */
  function probeOnlyServer(readiness: () => Promise<import("../src/server/readiness.js").ReadinessReport>) {
    return createHarnessServer({
      paths: resolveSmartCsPaths(),
      receipts: {} as ReceiptStore,
      memorySource: {} as MemorySourceStore,
      pythonClient: {} as PythonInternalClient,
      readiness,
    });
  }

  it("P11-R10: /health stays light, /ready mirrors the probe, and neither leaks internals", async () => {
    const server = probeOnlyServer(async () => ({
      ready: true,
      degraded: false,
      warnings: [],
      checks: { mysql: { ok: true }, business_runtime: { ok: true }, mcp: { enabled: false, ok: true } },
    }));
    const { port } = await server.listen(0, "127.0.0.1");
    try {
      const health = await fetch(`http://127.0.0.1:${port}/health`);
      expect(health.status).toBe(200);
      // Liveness keeps its original shape — it is not a dependency check.
      expect(await health.json()).toMatchObject({ ok: true });

      const ready = await fetch(`http://127.0.0.1:${port}/ready`);
      expect(ready.status).toBe(200);
      const body = (await ready.json()) as Record<string, unknown>;
      expect(body).toMatchObject({ ready: true, degraded: false });
      // No URL, no credential, no SQL, no driver text — only booleans and codes.
      expect(JSON.stringify(body)).not.toMatch(/https?:\/\/|password|secret|SELECT|bearer/i);
    } finally {
      await server.close();
    }
  });

  it("P11-R10b: a not-ready probe answers 503 with the same shape", async () => {
    const server = probeOnlyServer(async () => ({
      ready: false,
      degraded: false,
      warnings: [],
      checks: { mysql: { ok: false }, business_runtime: { ok: false }, mcp: { enabled: false, ok: false } },
    }));
    const { port } = await server.listen(0, "127.0.0.1");
    try {
      const response = await fetch(`http://127.0.0.1:${port}/ready`);
      expect(response.status).toBe(503);
      expect(await response.json()).toMatchObject({ ready: false });
    } finally {
      await server.close();
    }
  });

  it("P11-R10c: a probe that throws still answers — as not ready, never as a 500", async () => {
    const server = probeOnlyServer(async () => {
      throw new Error("probe bug");
    });
    const { port } = await server.listen(0, "127.0.0.1");
    try {
      const response = await fetch(`http://127.0.0.1:${port}/ready`);
      expect(response.status).toBe(503);
      expect(await response.json()).toMatchObject({ ready: false });
    } finally {
      await server.close();
    }
  });
});
