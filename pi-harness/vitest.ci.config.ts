import { defineConfig } from "vitest/config";

/**
 * The offline CI subset: no MySQL and no Python service. The workflow also
 * runs the full `npm test` suite in its integration job with real dependencies.
 *
 * WHY AN EXPLICIT LIST
 *
 * Most of this repository's vitest suites are integration tests by design: they
 * reset a real MySQL database and boot the real FastAPI `internal_api` in a
 * subprocess (see tests/README.md). The separate integration job provisions
 * both; this fast job intentionally has neither. Membership is decided here,
 * once, and every entry below is a file that was MEASURED to pass
 * with `MYSQL_HOST=127.0.0.1 MYSQL_PORT=1 MYSQL_PASSWORD=<invalid>
 * PYTHON_INTERNAL_BASE_URL=http://127.0.0.1:1`, i.e. with every external
 * dependency deliberately unreachable.
 *
 * WHY EACH FILE QUALIFIES (nothing below opens a socket or a connection)
 *
 *   smoke.test.ts                  process/import smoke only
 *   plan-deltas.test.ts            pure assertions over plan constants
 *   phase0-acceptance.test.ts      Faux provider + a temp session dir
 *   a1..a7, a8, a8b                SDK behaviour probes: Faux provider, temp
 *                                  session dirs, one child process for A5
 *   phase1-registry.test.ts        SessionRegistry mutex/eviction, in-process
 *   phase5d-live-wiring.test.ts    write-mode wiring decisions, pure
 *   phase8-skills.test.ts          skills disclosure from the repo's own files
 *   phase10-tool-surface.test.ts   prompt/tool-face composition, pure
 *   phase11-readiness.test.ts      injected dependency stubs + a local stub
 *                                  HTTP server standing in for the gateway
 *   phase11-outbox-ops.test.ts     receipt stub + pure CLI argument parsing
 *
 * The crash/state/transport matrices, memory-outbox chain, JWT interop and
 * other database/Python suites are covered by the full integration job, after
 * pytest finishes so the two runners never reset their shared schema together.
 */
const OFFLINE_SUITES = [
  "tests/smoke.test.ts",
  "tests/plan-deltas.test.ts",
  "tests/phase0-acceptance.test.ts",
  "tests/a1-session-id.test.ts",
  "tests/a2-message-end-replacement.test.ts",
  "tests/a3-event-order.test.ts",
  "tests/a4-durability.test.ts",
  "tests/a5-two-processes.test.ts",
  "tests/a6-agent-settled.test.ts",
  "tests/a7-reopen-session.test.ts",
  "tests/a8-tool-whitelist.test.ts",
  "tests/a8b-provider-events.test.ts",
  "tests/phase1-registry.test.ts",
  "tests/phase5d-live-wiring.test.ts",
  "tests/phase8-skills.test.ts",
  "tests/phase10-tool-surface.test.ts",
  "tests/copilot-profile.test.ts",
  "tests/phase11-readiness.test.ts",
  "tests/phase11-outbox-ops.test.ts",
];

export default defineConfig({
  test: {
    include: OFFLINE_SUITES,
    // Same reason as the main config: Pi sessions are filesystem-backed and the
    // A-series probes must not race each other over temp dirs.
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
