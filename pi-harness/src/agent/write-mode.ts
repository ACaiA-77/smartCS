/**
 * Write-mode switch (phase4-design.md §1).
 *
 *   off    (default) — the model never sees a write tool. Phase 0–3 behaviour.
 *   shadow           — write tools are visible and are intercepted: a plan row
 *                      is recorded and a canned result is returned. NOTHING is
 *                      executed and no internal HTTP call is made.
 *   live             — Phase 5. The write tools really call the runtime, which
 *                      alone decides whether the write is allowed to happen.
 *
 * The `shadow` and `live` tool definitions are the SAME objects; only the
 * execution branch differs. If the schemas diverged, shadow results would have
 * no predictive value for live.
 */

export type WriteMode = "off" | "shadow" | "live";

export const WRITE_MODE_ENV = "SMARTCS_WRITE_MODE";

export class LiveWriteWiringError extends Error {
  constructor(detail: string) {
    super(`SMARTCS_WRITE_MODE=live is enabled but the harness is not wired for it: ${detail}`);
    this.name = "LiveWriteWiringError";
  }
}

export function resolveWriteMode(raw = process.env[WRITE_MODE_ENV]): WriteMode {
  const value = (raw ?? "off").trim().toLowerCase();
  if (value === "" || value === "off") return "off";
  if (value === "shadow") return "shadow";
  if (value === "live") return "live";
  throw new Error(`invalid ${WRITE_MODE_ENV}: ${raw} (expected off|shadow|live)`);
}

/**
 * Fail fast when live mode is switched on without the pieces recovery needs.
 *
 * Live used to be rejected outright because it was unimplemented (Phase 4). It
 * is implemented from Phase 5d/5F, so the guard now checks WIRING rather than
 * refusing the mode: a live deployment without a durable receipt log would be
 * unable to reconcile a dropped write response, which is the one failure a
 * blind retry can turn into a duplicate side effect.
 */
export function assertWriteModeWired(mode: WriteMode, wiring: { durableOperationLog: boolean }): void {
  if (mode !== "live") return;
  if (!wiring.durableOperationLog) {
    throw new LiveWriteWiringError("no durable receipt store, so open write operations cannot be recovered");
  }
}

export function writeToolsEnabled(mode: WriteMode): boolean {
  return mode === "shadow" || mode === "live";
}
