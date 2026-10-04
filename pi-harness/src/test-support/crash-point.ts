/**
 * Programmable crash points (Phase 5F).
 *
 * The F1/F3/F5/F6 cases need a crash at an EXACT window: before the model is
 * consulted, before a WRITE leaves the harness, after the runtime confirmed a
 * WRITE, and after `final` was produced but before the receipt was completed.
 * Killing the process from outside can only approximate those windows, so the
 * harness carries a small, env-gated suicide point instead.
 *
 * Safety properties:
 *   * Inert unless `SMARTCS_CRASH_POINT` names a known window — production
 *     never sets it, so the calls are a string compare.
 *   * Works only inside a disposable process. It hard-aborts the CURRENT
 *     process, so it must never be armed in the vitest worker itself; the
 *     fault matrix arms it on a spawned harness child (see
 *     `tests/fixtures/matrix-harness.ts`).
 *   * Writes a durable marker BEFORE dying. A crash cannot report anything
 *     afterwards, and the test must be able to tell "the window was reached"
 *     from "the process died for some other reason".
 */

import { writeFileSync } from "node:fs";

export const CRASH_POINT_ENV = "SMARTCS_CRASH_POINT";
export const CRASH_MARKER_ENV = "SMARTCS_CRASH_MARKER";

export const CRASH_POINTS = [
  /** After identity/session/receipt work, before the model (and any tool) runs. */
  "before_llm_call",
  /** A live WRITE was authorized by the model turn; nothing was sent yet. */
  "before_write_send",
  /** The runtime returned a successful WRITE; the toolResult is not appended. */
  "after_write_success",
  /** `final` exists; the receipt is still `processing`. */
  "before_receipt_complete",
  /**
   * F15: the receipt is `completed` and the provenance row is durable, but the
   * memory outbox has not yet reached the runtime. Killing here is the exact
   * window the outbox exists to survive — nothing is lost, because the row
   * stays `pending` and the next process delivers it from durable state alone.
   */
  "before_memory_enqueue",
] as const;

export type CrashPoint = (typeof CRASH_POINTS)[number];

export function armedCrashPoint(raw = process.env[CRASH_POINT_ENV]): CrashPoint | undefined {
  const value = (raw ?? "").trim();
  return (CRASH_POINTS as readonly string[]).includes(value) ? (value as CrashPoint) : undefined;
}

/**
 * Kill this process *now* when `point` is the armed window.
 *
 * `process.abort()` is deliberate: no exit handlers, no stream flush, no
 * graceful shutdown — the same semantics a real crash has. A test that needs a
 * cleaner death should use the process-level kill instead.
 */
export function hitCrashPoint(point: CrashPoint): void {
  if (armedCrashPoint() !== point) return;

  const marker = process.env[CRASH_MARKER_ENV];
  if (marker) {
    try {
      writeFileSync(marker, JSON.stringify({ point, at: Date.now(), pid: process.pid }), "utf-8");
    } catch {
      /* the marker is diagnostic; the crash itself is the point */
    }
  }
  process.abort();
}
