/**
 * Shared test helpers.
 *
 * All tests run against the Faux provider (no network, no real LLM, no
 * Python endpoint) unless a test explicitly opts into the real provider.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerFauxProvider, type FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import type { FauxResponseStep } from "@earendil-works/pi-ai/providers/faux";
import { createSmartCsAgent, type CreateSmartCsAgentOptions, type SmartCsAgentHandle } from "../../src/agent/create-smartcs-agent.js";

export function makeTmpDir(prefix = "smartcs-phase0-"): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

export interface TestAgentHandle {
  agent: SmartCsAgentHandle;
  faux: FauxProviderRegistration;
  /** Temp root holding cwd/sessions/agent dirs. */
  root: string;
  /** Release runtime resources but KEEP the session files (reopen tests). */
  disposeSession: () => void;
  /** Release runtime resources and delete the temp root. */
  cleanup: () => void;
}

/**
 * Create a SmartCS agent backed by a fresh Faux provider and an isolated temp
 * session dir. Every call gets its own provider registration and its own
 * on-disk session root, so assertions cannot pass by leaking state.
 */
export interface TestAgentOptions extends Omit<CreateSmartCsAgentOptions, "faux" | "provider"> {
  /** Override the faux model definition (e.g. a tiny contextWindow to force compaction). */
  fauxModel?: { id?: string; contextWindow?: number; maxTokens?: number };
}

export async function createTestAgent(
  responses: FauxResponseStep[],
  options: TestAgentOptions = {},
): Promise<TestAgentHandle> {
  const { fauxModel, ...agentOptions } = options;
  const faux = registerFauxProvider({
    provider: "faux",
    api: "faux",
    models: [{ id: fauxModel?.id ?? "faux-1", name: "Faux 1", contextWindow: fauxModel?.contextWindow, maxTokens: fauxModel?.maxTokens }],
  });
  faux.setResponses(responses);

  const root = makeTmpDir();
  const agent = await createSmartCsAgent({
    provider: "faux",
    faux,
    // Phase 0/1 SDK-level tests use the hard-coded tool doubles; Phase 2
    // acceptance opts into "business" explicitly.
    toolMode: "fake",
    paths: {
      runtimeCwd: join(root, "cwd"),
      sessionDir: join(root, "sessions"),
      agentDir: join(root, "agent"),
    },
    ...agentOptions,
  });

  let disposed = false;
  const disposeSession = () => {
    if (disposed) return;
    disposed = true;
    agent.dispose();
    faux.unregister();
  };

  return {
    agent,
    faux,
    root,
    disposeSession,
    cleanup: () => {
      disposeSession();
      try {
        rmSync(root, { recursive: true, force: true });
      } catch {
        /* Windows can hold a handle briefly; temp dirs are disposable. */
      }
    },
  };
}
