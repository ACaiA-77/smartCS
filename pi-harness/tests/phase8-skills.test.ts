/**
 * Phase 8 §8.2 (P8-5/P8-6/P8-7): progressive skill disclosure.
 *
 * Three properties carry this section:
 *   * only the DESCRIPTION reaches the system prompt — the body is loaded on
 *     demand, which is what "progressive" means;
 *   * the disclosure survives compaction — the skills section is rebuilt from
 *     the resource loader each turn, not replayed from the transcript;
 *   * the default is off, and skills never add capability: the declared tool
 *     face is identical with skills on.
 */

import { afterEach, describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxToolCall, type FauxResponseStep } from "@earendil-works/pi-ai/providers/faux";
import { createTestAgent } from "./helpers/harness.js";

const SKILL_NAMES = ["cs-style", "refund-policy", "tool-guide"];
const DESCRIPTION_PHRASE = "退款政策口径与话术边界"; // refund-policy's description
const BODY_ONLY_PHRASE = "我帮您确认一下"; // appears only in cs-style's body

const saved = process.env.SMARTCS_SKILLS;
afterEach(() => {
  if (saved === undefined) delete process.env.SMARTCS_SKILLS;
  else process.env.SMARTCS_SKILLS = saved;
});

interface Probe {
  systemPrompt: string;
  declaredTools: string[];
  skillNames: string[];
}

interface ToolOutcome {
  toolName: string;
  isError: boolean;
  text: string;
}

/** Run one scripted tool call through a real agent and report what came back. */
async function callTool(toolName: string, arguments_: Record<string, unknown>): Promise<ToolOutcome> {
  const handle = await createTestAgent(
    [
      fauxToolCallStep(toolName, arguments_),
      fauxAssistantMessage("done"),
    ],
    { toolMode: "fake" },
  );
  const outcomes: ToolOutcome[] = [];
  const unsubscribe = handle.agent.session.subscribe((event) => {
    if (event.type === "tool_execution_end") {
      outcomes.push({
        toolName: event.toolName,
        isError: Boolean(event.isError),
        text: JSON.stringify(event.result ?? ""),
      });
    }
  });
  try {
    await handle.agent.session.prompt("测试技能加载");
  } finally {
    unsubscribe();
    handle.cleanup();
  }
  expect(outcomes).toHaveLength(1);
  return outcomes[0]!;
}

function fauxToolCallStep(name: string, arguments_: Record<string, unknown>): FauxResponseStep {
  return (() =>
    fauxAssistantMessage([fauxToolCall(name as never, arguments_ as never)], { stopReason: "toolUse" })) as FauxResponseStep;
}

function toolCollector(seen: string[]) {
  return (context: any) => {
    seen.push(
      ...(context?.messages ?? [])
        .filter((message: any) => message.role === "system")
        .flatMap((message: any) => (message.toolsAdded ?? []).map((tool: any) => tool.name)),
    );
    return fauxAssistantMessage("ok");
  };
}

async function probe(options: { contextWindow?: number; prompts?: number } = {}): Promise<Probe> {
  const declaredTools: string[] = [];
  const steps: FauxResponseStep[] = Array.from({ length: 8 }, () => toolCollector(declaredTools));
  const handle = await createTestAgent(steps, {
    toolMode: "fake",
    ...(options.contextWindow ? { fauxModel: { contextWindow: options.contextWindow, maxTokens: 256 } } : {}),
  });
  try {
    const prompts = options.prompts ?? 1;
    for (let index = 0; index < prompts; index += 1) {
      await handle.agent.session.prompt(`第 ${index + 1} 轮：请介绍退款政策。` + "补充说明。".repeat(index === 0 ? 1 : 40));
    }
    const loaded = handle.agent.resourceLoader.getSkills();
    return {
      systemPrompt: handle.agent.session.systemPrompt,
      declaredTools,
      skillNames: loaded.skills.map((skill) => skill.name).sort(),
    };
  } finally {
    handle.cleanup();
  }
}

describe("Phase 8 skills (§8.2)", () => {
  it("default: nothing is loaded and the prompt carries no skill text (P8-7)", async () => {
    delete process.env.SMARTCS_SKILLS;
    const result = await probe();
    expect(result.skillNames).toEqual([]);
    expect(result.systemPrompt).not.toContain(DESCRIPTION_PHRASE);
  });

  it("on: three skills are registered, only their descriptions are disclosed (P8-5)", async () => {
    process.env.SMARTCS_SKILLS = "on";
    const result = await probe();
    expect(result.skillNames).toEqual(SKILL_NAMES);
    expect(result.systemPrompt).toContain(DESCRIPTION_PHRASE);
    expect(result.systemPrompt).toContain("客服沟通风格与工单话术规范");
    expect(result.systemPrompt).toContain("knowledge_search 使用建议与工具选择说明");
    // Progressive disclosure: the body is not in the prompt.
    expect(result.systemPrompt).not.toContain(BODY_ONLY_PHRASE);
  });

  it("on: the tool face grows by exactly one, and only that one", async () => {
    // Approved boundary (Phase 8 §8.2): the model-side entry point for skill
    // bodies is `skill_load`, registered only while the switch is on.
    delete process.env.SMARTCS_SKILLS;
    const off = await probe();
    process.env.SMARTCS_SKILLS = "on";
    const on = await probe();
    expect(off.declaredTools.length).toBeGreaterThan(0);
    expect(off.declaredTools).not.toContain("skill_load");
    expect([...on.declaredTools].sort()).toEqual([...off.declaredTools, "skill_load"].sort());
  });

  it("on: skill_load returns the body, and only for a registered name", async () => {
    process.env.SMARTCS_SKILLS = "on";
    const body = await callTool("skill_load", { skill: "cs-style" });
    expect(body.isError, body.text).toBe(false);
    expect(body.text).toContain(BODY_ONLY_PHRASE);
    // The schema is an enum of the registered names: no path is expressible.
    const refused = await callTool("skill_load", { skill: "../../etc/passwd" });
    expect(refused.isError).toBe(true);
  }, 60_000);

  it("on: the disclosure survives a real compaction (P8-6)", async () => {
    process.env.SMARTCS_SKILLS = "on";
    const handleProbe = await probeWithTranscript({ contextWindow: 700 });
    // A compaction really happened, or the case proves nothing.
    expect(handleProbe.entryTypes.some((type) => type.includes("compaction"))).toBe(true);
    expect(handleProbe.promptAfter).toContain(DESCRIPTION_PHRASE);
  }, 60_000);
});

async function probeWithTranscript(options: { contextWindow: number }): Promise<{
  entryTypes: string[];
  promptAfter: string;
}> {
  const declaredTools: string[] = [];
  const steps: FauxResponseStep[] = Array.from({ length: 8 }, () => (context: any) => {
    declaredTools.push(
      ...(context?.messages ?? [])
        .filter((message: any) => message.role === "system")
        .flatMap((message: any) => (message.toolsAdded ?? []).map((tool: any) => tool.name)),
    );
    return fauxAssistantMessage("好的，已记录。" + "补充说明。".repeat(30));
  });
  const handle = await createTestAgent(steps, {
    toolMode: "fake",
    fauxModel: { contextWindow: options.contextWindow, maxTokens: 128 },
    // A profile this small is what makes a REAL compaction happen inside a
    // scripted turn; the stock profile reserves 8k and would never trigger.
    compaction: { enabled: true, reserveTokens: 150, keepRecentTokens: 100 },
  });
  try {
    await handle.agent.session.prompt("第一轮：请介绍退款政策。" + "背景材料。".repeat(120));
    await handle.agent.session.prompt("第二轮：那到账时间呢？" + "背景材料。".repeat(120));
    return {
      entryTypes: handle.agent.sessionManager.getEntries().map((entry: any) => String(entry.type)),
      promptAfter: handle.agent.session.systemPrompt,
    };
  } finally {
    handle.cleanup();
  }
}
