/**
 * Phase 6b: the test fixtures fail loudly instead of degrading silently.
 *
 * The acceptance round exposed a chain where a clean shell could start a
 * live-write runtime that could not see its platform schema; the two-phase
 * refund flow then quietly skipped its `pending_action` and surfaced much later
 * as an inexplicable refusal. These cases pin the loud behaviour.
 *
 * Deliberately database-free: both cases assert what the fixture does BEFORE
 * any test data matters.
 */

import { describe, expect, it } from "vitest";
import { childMysqlEnv, startPythonService } from "./helpers/phase1.js";

describe("Phase 6b fixture discipline", () => {
  it("refuses to start a live-write runtime that cannot see the platform schema", async () => {
    // A database name that does not exist: connecting/creating the schema must
    // fail at startup, and the fixture must say so instead of waiting 30s.
    await expect(
      startPythonService({
        port: 8_930,
        database: "smartcs_phase6b_missing",
        writeMode: "live",
      }),
    ).rejects.toThrow(/exited during startup|platform schema/i);
  }, 60_000);

  it("resolves the credentials a spawned service needs, or names what is missing", () => {
    // Happy path: the same resolution the application uses (process env first,
    // then python-impl/.env) — this is what a clean shell relies on.
    const env = childMysqlEnv();
    expect(env.MYSQL_HOST).toBeTruthy();
    expect(env.MYSQL_USER).toBeTruthy();
    expect(env.MYSQL_PASSWORD).toBeTruthy();
    expect(env.MYSQL_DATABASE).toBe(process.env.SMARTCS_TEST_DATABASE ?? "smartcs_phase1_test");
  });
});
