/**
 * Copilot profile acceptance (`SMARTCS_PROFILE=copilot`).
 *
 * Three invariants:
 *   * the profile switch defaults to e-commerce and rejects typos;
 *   * the Copilot prompt names exactly the tool surface that is mounted;
 *   * the Copilot tool schemas declare business parameters only — identity,
 *     authorization and idempotency fields belong to the runtime.
 *
 * Everything here is offline: the agent cases use the Faux provider and the
 * business tool mode never issues an HTTP request at construction time.
 */

import { afterEach, describe, expect, it } from "vitest";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { skillsDir } from "../src/agent/skills.js";
import { copilotPromptFor, copilotSystemPrompt } from "../src/agent/prompt/copilot.js";
import {
  COPILOT_READ_TOOL_NAMES,
  COPILOT_WRITE_TOOL_NAMES,
  createCopilotReadTools,
  createCopilotWriteTools,
} from "../src/agent/tools/copilot-tools.js";
import { TurnContext } from "../src/business/turn-context.js";
import type { PythonInternalClient } from "../src/business/python-client.js";
import { createTestAgent } from "./helpers/harness.js";

const COPLIOT_SKILL_NAMES = [
  "daily-briefing",
  "github-issue-triage",
  "mail-triage-reply",
  "obsidian-wiki-compile",
];

/** Fields that belong to the runtime; a model-supplied value is inert. */
const RUNTIME_ONLY_FIELDS = [
  "user_id",
  "confirmed",
  "approval_id",
  "operation_id",
  "idempotency_key",
  "uuid_key",
  "client_request_id",
  "request_payload_hash",
];

const ENV_KEYS = [
  "SMARTCS_SKILLS",
  "SMARTCS_SKILLS_DIR",
  "SMARTCS_KNOWLEDGE_TRANSPORT",
] as const;

const savedEnv = new Map<string, string | undefined>(ENV_KEYS.map((key) => [key, process.env[key]]));

afterEach(() => {
  for (const [key, value] of savedEnv) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

function clearProfileEnv(): void {
  for (const key of ENV_KEYS) delete process.env[key];
}

function schemaProperties(tool: ToolDefinition): Record<string, unknown> {
  const parameters = (tool as unknown as { parameters?: { properties?: Record<string, unknown> } }).parameters;
  return parameters?.properties ?? {};
}

function allCopilotTools(): ToolDefinition[] {
  const turnContext = new TurnContext();
  // The client is never called: this suite inspects declared schemas only.
  const client = {} as unknown as PythonInternalClient;
  return [
    ...createCopilotReadTools({ client, turnContext }),
    ...createCopilotWriteTools({ mode: "shadow", turnContext }),
  ];
}

describe("Copilot skills directory", () => {
  it("selects the copilot skills directory by default and respects overrides", () => {
    clearProfileEnv();
    expect(skillsDir()).toMatch(/skills-copilot[\\/]$/);
    // An explicit override always wins.
    expect(skillsDir("/tmp/custom")).toBe("/tmp/custom");
  });
});

describe("Copilot system prompt", () => {
  const readNames = [...COPILOT_READ_TOOL_NAMES];
  const writeNames = [...COPILOT_WRITE_TOOL_NAMES];

  it("names every READ and WRITE tool when writes are mounted", () => {
    const prompt = copilotSystemPrompt({ writeTools: true });
    for (const name of [...readNames, ...writeNames]) {
      expect(prompt, `prompt must name ${name}`).toContain(name);
    }
    expect(prompt).toContain("无权自行授权");
    expect(prompt).toContain("等待用户明确回复确认");
    expect(prompt).toContain("pending_action_id");
  });

  it("never advertises a write tool when writes are disabled", () => {
    const prompt = copilotSystemPrompt({ writeTools: false });
    for (const name of writeNames) {
      expect(prompt, `prompt must not advertise ${name} when writes are off`).not.toContain(name);
    }
    expect(prompt).toContain("未启用");
    for (const name of readNames) expect(prompt).toContain(name);
  });

  it("renames the knowledge tool for the MCP transport, in every occurrence", () => {
    const bare = copilotSystemPrompt({ writeTools: true });
    const renamed = copilotPromptFor("mcp__knowledge__knowledge_search", { writeTools: true });
    const occurrences = (text: string, needle: string) => text.split(needle).length - 1;

    expect(renamed).toContain("mcp__knowledge__knowledge_search");
    expect(occurrences(renamed, "mcp__knowledge__knowledge_search")).toBe(
      occurrences(bare, "knowledge_search"),
    );
    expect(copilotPromptFor("knowledge_search", { writeTools: true })).toBe(bare);
  });
});

describe("Copilot tool surface", () => {
  it("exposes exactly the read and write tool names", () => {
    expect(allCopilotTools().map((tool) => tool.name).sort()).toEqual(
      [...COPILOT_READ_TOOL_NAMES, ...COPILOT_WRITE_TOOL_NAMES].sort(),
    );
  });

  it("declares business parameters only — no runtime-owned field appears anywhere", () => {
    for (const tool of allCopilotTools()) {
      const properties = Object.keys(schemaProperties(tool));
      for (const field of RUNTIME_ONLY_FIELDS) {
        expect(properties, `${tool.name} must not declare ${field}`).not.toContain(field);
      }
    }
  });

  it("keeps every schema closed to unknown fields", () => {
    for (const tool of allCopilotTools()) {
      const parameters = (tool as unknown as { parameters: { additionalProperties?: boolean } }).parameters;
      expect(parameters.additionalProperties, `${tool.name} must stay closed`).toBe(false);
    }
  });

  it("pins the parameter set for the two-phase external action tools", () => {
    const expected: Record<string, string[]> = {
      mail_save_draft: ["account", "to", "subject", "body", "cc", "reply_to_message_id"],
      copilot_action_prepare: ["tool", "arguments"],
      copilot_action_confirm: ["pending_action_id"],
      vault_propose_edit: ["target_rel_path", "rationale", "proposed_content"],
    };
    const byName = new Map(allCopilotTools().map((tool) => [tool.name, tool]));
    for (const [name, fields] of Object.entries(expected)) {
      expect(Object.keys(schemaProperties(byName.get(name)!)).sort(), name).toEqual(fields.sort());
    }
  });
});

describe("Copilot agent assembly", () => {
  it("mounts the copilot prompt, tool face and skills", async () => {
    clearProfileEnv();
    process.env.SMARTCS_SKILLS = "on";

    const copilot = await createTestAgent([fauxAssistantMessage("ok")], {
      toolMode: "business",
    });
    try {
      expect(copilot.agent.session.systemPrompt).toContain("个人受控 Copilot");

      const active = copilot.agent.session.getActiveToolNames().sort();
      // Write mode defaults to off: exactly the READ surface plus skill_load.
      expect(active).toEqual([...COPILOT_READ_TOOL_NAMES, "skill_load"].sort());
      for (const name of COPILOT_WRITE_TOOL_NAMES) expect(active).not.toContain(name);

      const skillNames = copilot.agent.resourceLoader
        .getSkills()
        .skills.map((skill) => skill.name)
        .sort();
      expect(skillNames).toEqual(COPLIOT_SKILL_NAMES);
      expect(copilot.agent.session.systemPrompt).toContain("obsidian-wiki-compile");
      // Progressive disclosure: the body is not in the prompt.
      expect(copilot.agent.session.systemPrompt).not.toContain("<SHADOW>");
    } finally {
      copilot.cleanup();
    }
  }, 60_000);

  it("mounts copilot write tools when shadow mode is wired", async () => {
    clearProfileEnv();
    process.env.SMARTCS_SKILLS = "off";

    const records: string[] = [];
    const store = {
      async record(plan: { toolName: string }) {
        records.push(plan.toolName);
        return 1;
      },
    } as unknown as import("../src/db/shadow-plans.js").ShadowPlanStore;

    const handle = await createTestAgent([fauxAssistantMessage("ok")], {
      toolMode: "business",
      writeMode: "shadow",
      shadowPlanStore: store,
    });
    try {
      const active = handle.agent.session.getActiveToolNames().sort();
      expect(active).toEqual([...COPILOT_READ_TOOL_NAMES, ...COPILOT_WRITE_TOOL_NAMES].sort());
      expect(handle.agent.session.systemPrompt).toContain("copilot_action_prepare");
    } finally {
      handle.cleanup();
    }
    expect(records).toEqual([]);
  }, 60_000);
});
