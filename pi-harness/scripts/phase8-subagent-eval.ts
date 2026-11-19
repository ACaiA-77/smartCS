/**
 * Phase 8 §8.3 — measurement harness for the subagent evaluation.
 *
 * It drives the PRODUCTION assembly (`createSmartCsAgent`, Faux provider) and
 * captures what the model actually receives: the system prompt and the tool
 * declarations of that turn. Nothing is simulated about the prompt; only the
 * model is scripted. Token counts are produced by `scripts/phase8-count-tokens.py`
 * (tiktoken cl100k_base — the same tokenizer the Python context manager uses).
 *
 *   npx tsx scripts/phase8-subagent-eval.ts > .runtime/phase8/subagent-eval.json
 *   python scripts/phase8-count-tokens.py .runtime/phase8/subagent-eval.json
 *
 * Configs:
 *   full          — today's single Main Agent: every READ tool declared
 *   rag-only      — a specialist that would serve a knowledge question
 *   transaction   — a specialist that would serve an order/refund turn
 *   full+8/full+23— the same full face plus synthetic tools, for the
 *                   tool-surface sensitivity curve (7 → 15 → 30)
 */

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import { registerFauxProvider } from "@earendil-works/pi-ai/compat";
import { createSmartCsAgent } from "../src/agent/create-smartcs-agent.js";
import { COPILOT_READ_TOOL_NAMES } from "../src/agent/tools/copilot-tools.js";
import { PythonInternalClient } from "../src/business/python-client.js";
import { TurnContext } from "../src/business/turn-context.js";
import { makeTmpDir } from "../tests/helpers/harness.js";

const RAG_QUESTION = "我们内部关于 Context Engineering 的设计要点有哪些？";
const WORKSPACE_TURN = "帮我查一下 Obsidian 里的项目 Alpha 笔记和本体关系，如果缺索引就准备发邮件提醒。";
/** A stand-in for the turn snapshot the pipeline injects (Phase 3). */
const TURN_SNAPSHOT = (user: string) =>
  `[Copilot 工作台上下文快照 · 权威数据，请以此为准]\n用户：${user}\n待确认外部操作：无\n`;

function fillerTools(count: number): ToolDefinition[] {
  const areas = ["inventory", "logistics", "coupon", "invoice", "member", "campaign", "warehouse", "supplier"];
  return Array.from({ length: count }, (_, index) => {
    const area = areas[index % areas.length];
    return defineTool({
      name: `ops_${area}_${index + 1}`,
      label: `${area} 运维查询 ${index + 1}`,
      description: `内部运维接口：查询 ${area} 域的明细记录，供客服核对后台数据使用。`,
      parameters: Type.Object(
        {
          record_id: Type.String({ description: "记录号" }),
          detail_level: Type.Optional(Type.String({ description: "明细层级" })),
          include_history: Type.Optional(Type.Boolean({ description: "是否包含历史" })),
        },
        { additionalProperties: false },
      ),
      async execute() {
        return { content: [{ type: "text" as const, text: "{}" }], details: {} };
      },
    }) as unknown as ToolDefinition;
  });
}

interface Capture {
  config: string;
  scenario: string;
  systemPrompt: string;
  tools: Array<{ name: string; schema: string }>;
  userMessage: string;
  snapshot: string;
}

async function capture(
  config: string,
  scenario: string,
  options: { keep: (name: string) => boolean; fillers: number; whitelist?: string[] },
): Promise<Capture> {
  const faux = registerFauxProvider({ provider: "faux", api: "faux", models: [{ id: "faux-1", name: "Faux 1" }] });
  let seenSystemPrompt = "";
  let seenTools: Array<{ name: string; schema: string }> = [];
  faux.setResponses([
    (context: any) => {
      seenTools = (context?.messages ?? [])
        .filter((message: any) => message.role === "system")
        .flatMap((message: any) => (message.toolsAdded ?? []))
        .map((tool: any) => ({ name: String(tool.name), schema: JSON.stringify(tool) }));
      return {
        role: "assistant",
        content: [{ type: "text", text: "好的。" }],
        stopReason: "stop",
        timestamp: Date.now(),
      } as any;
    },
  ]);

  const root = makeTmpDir("p8-eval-");
  const turnContext = new TurnContext();
  const client = new PythonInternalClient({ baseUrl: "http://127.0.0.1:9" });
  const additionalTools = fillerTools(options.fillers);
  const agent = await createSmartCsAgent({
    provider: "faux",
    faux,
    toolMode: "business",
    pythonClient: client,
    turnContext,
    ...(options.whitelist ? { tools: options.whitelist } : {}),
    ...(additionalTools.length ? { additionalTools } : {}),
    paths: { runtimeCwd: join(root, "cwd"), sessionDir: join(root, "sessions"), agentDir: join(root, "agent") },
  });
  try {
    turnContext.set({
      accountId: 1,
      businessUserId: "bu-eval",
      sessionId: "session-eval",
      clientRequestId: "request-eval",
      agentRunId: "42",
    });
    const snapshot = TURN_SNAPSHOT("bu-eval");
    agent.snapshotHolder?.set?.(snapshot);
    const userMessage = scenario === "rag" ? RAG_QUESTION : WORKSPACE_TURN;
    await agent.session.prompt(userMessage);
    seenSystemPrompt = agent.session.systemPrompt;
    return { config, scenario, systemPrompt: seenSystemPrompt, tools: seenTools, userMessage, snapshot };
  } finally {
    agent.dispose();
    faux.unregister();
  }
}

async function main(): Promise<void> {
  const knowledge = (name: string) => ["knowledge_search", "vault_search"].includes(name);
  const workspace = (name: string) => ["vault_search", "vault_read_note", "onto_query"].includes(name);
  const everything = () => true;

  const captures: Capture[] = [];
  for (const scenario of ["rag", "workspace"]) {
    captures.push(await capture("full", scenario, { keep: everything, fillers: 0 }));
    captures.push(await capture("specialist", scenario, {
      keep: scenario === "rag" ? knowledge : workspace,
      fillers: 0,
      whitelist: scenario === "rag" ? ["knowledge_search", "vault_search"] : ["vault_search", "vault_read_note", "onto_query"],
    }));
  }
  // Tool-surface sensitivity on the SAME scenario: 7 -> 15 -> 30 declared tools.
  captures.push(await capture("full+8", "workspace", { keep: everything, fillers: 8 }));
  captures.push(await capture("full+23", "workspace", { keep: everything, fillers: 23 }));

  const payload = {
    generatedBy: "scripts/phase8-subagent-eval.ts",
    readToolNames: [...COPILOT_READ_TOOL_NAMES],
    captures,
  };
  const runtimeDir = join(process.cwd(), ".runtime", "phase8");
  mkdirSync(runtimeDir, { recursive: true });
  process.stdout.write(JSON.stringify(payload));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
