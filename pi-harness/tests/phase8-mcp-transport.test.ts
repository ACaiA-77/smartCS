/**
 * Phase 8 §8.1 (P8-1/P8-2/P8-3): the knowledge transport switch.
 *
 * Real stack: pi's built-in MCP client -> the real Python gateway process
 * (token guard, real tool definition) -> the isolated corpus. The model is the
 * Faux provider, scripted to call a tool by name, so what these cases observe is
 * exactly the tool face each transport declares and what a call returns.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { fauxAssistantMessage, fauxToolCall, type FauxResponseStep } from "@earendil-works/pi-ai/providers/faux";
import { PythonInternalClient } from "../src/business/python-client.js";
import {
  KNOWLEDGE_HTTP_TOOL,
  KNOWLEDGE_MCP_TOOL,
} from "../src/agent/mcp/knowledge-mcp.js";
import { createTestAgent } from "./helpers/harness.js";
import { MCP_TEST_CORPUS_ANSWER, MCP_TEST_TOKEN, startMcpGateway, type McpGateway } from "./helpers/mcp-gateway.js";

const PORT = 9_180;
let gateway: McpGateway;

beforeAll(async () => {
  gateway = await startMcpGateway({ port: PORT });
}, 180_000);

afterAll(async () => {
  await gateway?.stop();
});

interface ToolOutcome {
  toolName: string;
  isError: boolean;
  text: string;
}

/** Run one scripted tool call through a real agent and report what came back. */
async function callTool(toolName: string, arguments_: Record<string, unknown>): Promise<ToolOutcome> {
  const steps: FauxResponseStep[] = [
    fauxAssistantMessage([fauxToolCall(toolName as never, arguments_ as never)], { stopReason: "toolUse" }),
    fauxAssistantMessage("done"),
  ];
  const handle = await createTestAgent(steps, {
    toolMode: "business",
    // This tool must never need the Business Runtime; the other shells are not
    // exercised here.
    pythonClient: new PythonInternalClient({ baseUrl: "http://127.0.0.1:9" }),
  });
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
    await handle.agent.session.prompt("测试知识检索");
  } finally {
    unsubscribe();
    handle.cleanup();
  }
  expect(outcomes).toHaveLength(1);
  return outcomes[0]!;
}

function withMcpEnv<T>(token: string, body: () => Promise<T>): Promise<T> {
  const saved = {
    transport: process.env.SMARTCS_KNOWLEDGE_TRANSPORT,
    url: process.env.SMARTCS_MCP_URL,
    token: process.env.SMARTCS_MCP_TOKEN,
  };
  process.env.SMARTCS_KNOWLEDGE_TRANSPORT = "mcp";
  process.env.SMARTCS_MCP_URL = gateway.url;
  process.env.SMARTCS_MCP_TOKEN = token;
  return body().finally(() => {
    for (const [key, value] of [
      ["SMARTCS_KNOWLEDGE_TRANSPORT", saved.transport],
      ["SMARTCS_MCP_URL", saved.url],
      ["SMARTCS_MCP_TOKEN", saved.token],
    ] as const) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
}

describe("Phase 8 knowledge transport (§8.1)", () => {
  it("mcp: the MCP tool is declared and reaches the real gateway", async () => {
    const outcome = await withMcpEnv(MCP_TEST_TOKEN, () =>
      callTool(KNOWLEDGE_MCP_TOOL, { query: "退款政策", top_k: 2 }),
    );
    expect(outcome.isError, outcome.text).toBe(false);
    expect(outcome.text).toContain(MCP_TEST_CORPUS_ANSWER);
  }, 120_000);

  it("mcp: the HTTP knowledge shell is not declared at the same time", async () => {
    const outcome = await withMcpEnv(MCP_TEST_TOKEN, () =>
      callTool(KNOWLEDGE_HTTP_TOOL, { query: "退款政策" }),
    );
    // Unknown tool: exactly one knowledge tool is on the face, whichever
    // transport is selected.
    expect(outcome.isError).toBe(true);
  }, 120_000);

  it("mcp: a wrong server token is refused end to end", async () => {
    const outcome = await withMcpEnv("phase8-wrong-token-000000000", () =>
      callTool(KNOWLEDGE_MCP_TOOL, { query: "退款政策" }),
    );
    expect(outcome.isError).toBe(true);
    expect(outcome.text).not.toContain(MCP_TEST_CORPUS_ANSWER);
  }, 120_000);

  it("default: no MCP tool is mounted without the switch (P8-3)", async () => {
    const saved = process.env.SMARTCS_KNOWLEDGE_TRANSPORT;
    delete process.env.SMARTCS_KNOWLEDGE_TRANSPORT;
    try {
      const outcome = await callTool(KNOWLEDGE_MCP_TOOL, { query: "退款政策" });
      expect(outcome.isError).toBe(true);
    } finally {
      if (saved !== undefined) process.env.SMARTCS_KNOWLEDGE_TRANSPORT = saved;
    }
  }, 120_000);
});
