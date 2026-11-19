/**
 * Shared live-write execution boundary.
 *
 * All live Copilot writes (`vault_*`, `wiki_*`, `onto_*`, `mail_save_draft`,
 * `copilot_action_confirm`) cross the same boundary: the runtime authorizes and
 * executes, and the harness makes the OUTCOME authoritative. Duplicating that
 * logic per tool family would mean two chances to skip crash points or to retry
 * a write whose transport failed — the one mistake that can duplicate an
 * external side effect. So it lives here, once.
 */

import type { AgentToolResult } from "@earendil-works/pi-coding-agent";
import type { PythonInternalClient } from "../../business/python-client.js";
import type { TurnIdentity } from "../../business/turn-context.js";
import type { ReceiptStore } from "../../db/receipts.js";
import { hitCrashPoint } from "../../test-support/crash-point.js";

const EXTERNAL_ACTION_TOOLS = new Set([
  "mail_send",
  "gh_create_issue",
  "gh_post_comment",
  "feishu_send_message",
]);

/** The pieces a live write needs: the runtime client and the durable ledger. */
export interface LiveWriteRuntime {
  client: PythonInternalClient;
  receipts: ReceiptStore;
}

/**
 * Execute a real write through the runtime, then make the OUTCOME authoritative.
 *
 * The dangerous case is a transport failure: the write may or may not have
 * happened. The harness never guesses there — it finds the operation id from
 * the durable receipt record (written by the runtime BEFORE sending) and asks
 * the ledger. A blind retry would be the one thing that can duplicate an
 * outbound mail or external action, so an unresolved outcome surfaces as an
 * error, never as "try again".
 */
export async function executeLiveWrite(
  runtime: LiveWriteRuntime,
  identity: TurnIdentity,
  toolName: string,
  params: Record<string, unknown>,
  signal?: AbortSignal,
  toolCallId?: string,
): Promise<AgentToolResult<unknown>> {
  // F3 window: the model decided to write, nothing has left this process. The
  // runtime has not reserved an operation, so a resend is safe by definition.
  hitCrashPoint("before_write_send");

  try {
    const response = await runtime.client.executeTool({
      tool: toolName,
      arguments: params,
      identity,
      toolCallId,
      signal,
    });
    const details = (response.details ?? {}) as Record<string, unknown>;
    // F5 window: the runtime reports the side effect as executed, so it IS
    // durable — but Pi has not appended the toolResult. This is the one place
    // where "it happened but the transcript does not know" is true.
    if (details.authorized === true && details.executed === true) hitCrashPoint("after_write_success");
    // A business refusal is a normal result the model must see.
    return { content: [{ type: "text" as const, text: response.content }], details };
  } catch (error) {
    return reconcileAfterTransportFailure(runtime, toolName, identity, error);
  }
}

/**
 * Resolve a write whose HTTP call failed, from the ledger — never by guessing.
 *
 * `COMPLETED` finishes the request from the authority; `FAILED` reports a
 * deterministic failure with no business change; everything else (UNKNOWN or
 * PROVABLY_NOT_EXECUTED) stops automatic processing, because retry policy is
 * not a transport concern.
 */
export async function reconcileAfterTransportFailure(
  runtime: LiveWriteRuntime,
  toolName: string,
  identity: TurnIdentity,
  error: unknown,
): Promise<AgentToolResult<unknown>> {
  const operations = await runtime.receipts.openWriteOperations(identity.sessionId, identity.clientRequestId);
  const operation = [...operations]
    .reverse()
    .find(
      (item) =>
        item.tool === toolName ||
        (toolName === "copilot_action_confirm" && EXTERNAL_ACTION_TOOLS.has(item.tool)),
    );

  if (!operation) {
    // No durable record ⇒ the write was never sent. Failing is correct and
    // safe: the user can simply retry.
    throw new Error(
      `工具 ${toolName} 未能提交（未发出任何写操作）：${error instanceof Error ? error.message : String(error)}`,
    );
  }

  const verdict = await runtime.client.operationStatus({ identity, operationId: operation.operation_id });

  if (verdict.status === "COMPLETED") {
    // Deterministic recovery (design §4 / F5): the side effect DID happen, so
    // finish the request from the authority instead of replaying anything.
    return {
      content: [
        {
          type: "text" as const,
          text: `[业务已完成] ${toolName} 已由业务系统执行完成（恢复自账簿，未重复执行）。`,
        },
      ],
      details: {
        recovered: true,
        executed: true,
        operationId: operation.operation_id,
        ledgerStatus: verdict.status,
        result: verdict.result,
      },
    };
  }

  if (verdict.status === "FAILED") {
    throw new Error(`工具 ${toolName} 执行失败（账簿权威结论），未产生任何业务变更。`);
  }

  // UNKNOWN or PROVABLY_NOT_EXECUTED: do not act automatically.
  throw new Error(
    `工具 ${toolName} 结果未确定（${verdict.status}），已停止自动处理，需人工核对后再决定。`,
  );
}
