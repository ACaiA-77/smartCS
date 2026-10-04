/**
 * Phase 5d: the TS live wiring's state machine.
 *
 * Deterministic stubs rather than a live runtime: what is under test here is the
 * HARNESS's decision logic — when it replays, when it reconciles, and above all
 * when it refuses to act. The Python authority itself is covered by the Python
 * suites (5a/5b/5c).
 */

import { describe, expect, it } from "vitest";
import { createShadowWriteTools } from "../src/agent/tools/shadow-write-tools.js";
import { TurnContext } from "../src/business/turn-context.js";
import type { PythonInternalError } from "../src/business/python-client.js";

const IDENTITY = {
  accountId: 1,
  businessUserId: "user_002",
  sessionId: "sess-5d",
  clientRequestId: "req-5d",
};

interface StubOptions {
  executeTool?: () => Promise<{ ok: boolean; content: string; details: unknown }>;
  operations?: Array<{ operation_id: string; tool: string; state: string; target_hash: string }>;
  verdict?: { status: string; detail: string; result: unknown };
}

function buildTool(options: StubOptions) {
  const turnContext = new TurnContext();
  turnContext.set(IDENTITY);
  const calls: Array<{ tool: string; operationId: string }> = [];

  const client = {
    async executeTool() {
      if (!options.executeTool) throw new Error("no stub");
      return options.executeTool();
    },
    async operationStatus(params: { operationId: string }) {
      calls.push({ tool: "operation_status", operationId: params.operationId });
      return options.verdict ?? { status: "UNKNOWN", detail: "stub", result: null };
    },
  } as never;

  const receipts = {
    async openWriteOperations() {
      return options.operations ?? [];
    },
  } as never;

  const [tool] = createShadowWriteTools({
    mode: "live",
    store: { async record() { return 1; } } as never,
    turnContext,
    client,
    receipts,
  });
  return { tool: tool!, calls };
}

const args = { pending_action_id: "pa-1" };

describe("Phase 5d live wiring", () => {
  it("passes a successful write through and surfaces the runtime's details", async () => {
    const { tool } = buildTool({
      executeTool: async () => ({
        ok: true,
        content: "退款已提交",
        details: { authorized: true, executed: true, operationId: "op-1" },
      }),
    });
    const result = (await (tool.execute as any)("call-1", args, undefined, undefined, {})) as {
      details: Record<string, unknown>;
    };
    expect(result.details.operationId).toBe("op-1");
    expect(result.details.executed).toBe(true);
  });

  it("a business refusal is returned to the model, not thrown", async () => {
    const { tool } = buildTool({
      executeTool: async () => ({
        ok: true,
        content: "未检测到明确的退款确认",
        details: { authorized: false, executed: false, errorCode: "explicit_confirmation_required" },
      }),
    });
    const result = (await (tool.execute as any)("call-2", args, undefined, undefined, {})) as {
      details: Record<string, unknown>;
    };
    expect(result.details.authorized).toBe(false);
    expect(result.details.errorCode).toBe("explicit_confirmation_required");
  });

  it("transport failure with NO durable operation refuses to guess", async () => {
    const { tool, calls } = buildTool({
      executeTool: async () => {
        throw new Error("socket hang up") as unknown as PythonInternalError;
      },
      operations: [],
    });
    await expect((tool.execute as any)("call-3", args, undefined, undefined, {})).rejects.toThrow(
      /未能提交/,
    );
    expect(calls).toHaveLength(0); // never even asked: nothing was sent
  });

  it("F5: a COMPLETED ledger finishes the request deterministically without replay", async () => {
    const { tool, calls } = buildTool({
      executeTool: async () => {
        throw new Error("socket hang up") as unknown as PythonInternalError;
      },
      operations: [{ operation_id: "op-9", tool: "refund_confirm", state: "PREPARED", target_hash: "h" }],
      verdict: { status: "COMPLETED", detail: "ledger recorded a completed execution", result: { refund_id: 7 } },
    });

    const result = (await (tool.execute as any)("call-4", args, undefined, undefined, {})) as {
      content: Array<{ text: string }>;
      details: Record<string, unknown>;
    };

    expect(calls).toEqual([{ tool: "operation_status", operationId: "op-9" }]);
    expect(result.details.recovered).toBe(true);
    expect(result.details.executed).toBe(true);
    expect(result.details.result).toEqual({ refund_id: 7 });
    // The recovery is stated in the transcript so the model does not re-ask.
    expect(result.content[0]!.text).toContain("业务已完成");
  });

  it("P5-8: an aborted request is resolved by the ledger, not by the abort", async () => {
    // The signal fires mid-flight; the shell must not treat that as failure.
    const controller = new AbortController();
    controller.abort();

    const { tool } = buildTool({
      executeTool: async () => {
        throw new Error("request aborted") as unknown as PythonInternalError;
      },
      operations: [{ operation_id: "op-ab", tool: "refund_confirm", state: "PREPARED", target_hash: "h" }],
      verdict: { status: "COMPLETED", detail: "completed", result: { refund_id: 8 } },
    });

    const result = (await (tool.execute as any)("call-5", args, controller.signal, undefined, {})) as {
      details: Record<string, unknown>;
    };
    // Abort ≠ business failure: the ledger says the refund happened.
    expect(result.details.executed).toBe(true);
    expect(result.details.ledgerStatus).toBe("COMPLETED");
  });

  it("UNKNOWN is surfaced, never retried", async () => {
    const { tool, calls } = buildTool({
      executeTool: async () => {
        throw new Error("timeout") as unknown as PythonInternalError;
      },
      operations: [{ operation_id: "op-u", tool: "refund_confirm", state: "PREPARED", target_hash: "h" }],
      verdict: { status: "UNKNOWN", detail: "ledger claim is in_progress", result: null },
    });

    await expect((tool.execute as any)("call-6", args, undefined, undefined, {})).rejects.toThrow(
      /结果未确定/,
    );
    // Exactly one reconcile, and no second execution attempt.
    expect(calls).toHaveLength(1);
  });

  it("a FAILED ledger is reported as a definitive failure", async () => {
    const { tool } = buildTool({
      executeTool: async () => {
        throw new Error("boom") as unknown as PythonInternalError;
      },
      operations: [{ operation_id: "op-f", tool: "refund_confirm", state: "PREPARED", target_hash: "h" }],
      verdict: { status: "FAILED", detail: "ledger recorded a failed execution", result: null },
    });
    await expect((tool.execute as any)("call-7", args, undefined, undefined, {})).rejects.toThrow(
      /执行失败/,
    );
  });
});

