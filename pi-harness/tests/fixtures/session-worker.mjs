/**
 * Child-process fixture for A5 (two processes, one session id).
 *
 * Modes:
 *   create  — SessionManager.create(cwd, dir, {id}) + one user message
 *   open    — SessionManager.open(path) + one user message
 *
 * Prints a single JSON line on stdout so the parent can classify the outcome.
 * Any failure is reported as { ok:false, error } rather than crashing loudly,
 * because the crash itself is the observation under test.
 */
import { mkdirSync } from "node:fs";
import { SessionManager } from "@earendil-works/pi-coding-agent";

const [, , mode, sessionId, sessionDir, cwd, payload, barrierRaw] = process.argv;
const barrier = Number(barrierRaw ?? "0");

/** Busy-wait until a wall-clock millisecond boundary so two processes collide. */
function waitForBarrier() {
  if (!barrier) return;
  while (Date.now() < barrier) {
    // spin — precision matters more than politeness in a 1ms window
  }
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

try {
  mkdirSync(cwd, { recursive: true });
  mkdirSync(sessionDir, { recursive: true });
  waitForBarrier();

  if (mode === "create") {
    const sm = SessionManager.create(cwd, sessionDir, { id: sessionId });
    const file = sm.getSessionFile();
    sm.appendMessage({ role: "user", content: payload, timestamp: Date.now() });
    emit({ ok: true, pid: process.pid, id: sm.getSessionId(), file, openedExisting: false });
  } else if (mode === "open") {
    // Retry briefly so the opener can race the creator deterministically.
    let sm;
    for (let attempt = 0; attempt < 200; attempt += 1) {
      const found = SessionManager.findById(cwd, sessionId, sessionDir);
      if (found) {
        sm = SessionManager.open(found);
        break;
      }
      await new Promise((r) => setTimeout(r, 10));
    }
    if (!sm) throw new Error("session file never appeared");
    sm.appendMessage({ role: "user", content: payload, timestamp: Date.now() });
    emit({ ok: true, pid: process.pid, id: sm.getSessionId(), file: sm.getSessionFile(), openedExisting: true });
  } else {
    throw new Error(`unknown mode: ${mode}`);
  }
} catch (error) {
  emit({ ok: false, error: String(error && error.message ? error.message : error), code: error?.code });
}
