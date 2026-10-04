/**
 * Pi session lifecycle: "先查后建" (look up, then create).
 *
 * Phase 0 D4: `SessionManager.create(cwd, dir, {id})` accepts any valid id but
 * the id is NOT unique within a directory (two creates → two files sharing an
 * id), and `SessionManager.open()` silently creates a NEW empty session when
 * the path does not exist (Phase 0 A7 hazard). So every reopen goes through:
 *
 *   findById → (found) verify file exists + id matches → open
 *            → (absent) create
 *
 * and never trusts `open()` to fail on a missing file.
 */

import { existsSync } from "node:fs";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { createSmartCsAgent, type CreateSmartCsAgentOptions, type SmartCsAgentHandle } from "../agent/create-smartcs-agent.js";
import type { SmartCsPaths } from "../config/env.js";

export class PiSessionLookupError extends Error {}

export interface ResolvedPiSession {
  sessionManager: SessionManager;
  /** "opened" = an existing file was reused; "created" = a new transcript. */
  origin: "opened" | "created";
  file: string | undefined;
}

/** Build a SessionManager for `sessionId`, reusing the existing transcript if any. */
export function openOrCreateSessionManager(paths: SmartCsPaths, sessionId: string): ResolvedPiSession {
  const found = SessionManager.findById(paths.runtimeCwd, sessionId, paths.sessionDir);
  if (found) {
    // findById reads headers off disk, but re-check explicitly: the contract
    // here is "never let open() create a surprise empty session".
    if (!existsSync(found)) {
      throw new PiSessionLookupError(`session file disappeared between lookup and open: ${found}`);
    }
    const manager = SessionManager.open(found, paths.sessionDir, paths.runtimeCwd);
    if (manager.getSessionId() !== sessionId) {
      throw new PiSessionLookupError(
        `session id mismatch: expected ${sessionId}, file holds ${manager.getSessionId()}`,
      );
    }
    return { sessionManager: manager, origin: "opened", file: manager.getSessionFile() };
  }

  const manager = SessionManager.create(paths.runtimeCwd, paths.sessionDir, { id: sessionId });
  if (manager.getSessionId() !== sessionId) {
    throw new PiSessionLookupError(`created session id mismatch: ${manager.getSessionId()}`);
  }
  return { sessionManager: manager, origin: "created", file: manager.getSessionFile() };
}

export interface PiSessionHandle extends SmartCsAgentHandle {
  origin: "opened" | "created";
}

/** Full runtime handle (agent + session) for one SmartCS session id. */
export async function openOrCreatePiSession(
  paths: SmartCsPaths,
  sessionId: string,
  options: Omit<CreateSmartCsAgentOptions, "sessionManager" | "sessionId" | "paths"> = {},
): Promise<PiSessionHandle> {
  const { sessionManager, origin } = openOrCreateSessionManager(paths, sessionId);
  const handle = await createSmartCsAgent({
    ...options,
    paths,
    sessionManager,
  });
  return { ...handle, origin };
}
