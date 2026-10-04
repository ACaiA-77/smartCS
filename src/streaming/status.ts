/**
 * Streaming primitives for Phase 0.
 *
 * Plan v2 §6.5 — Compliance-First Streaming:
 *  - the realtime channel carries only DETERMINISTIC status derived from tool
 *    name / runtime state; model narration must never leak as status;
 *  - the final answer is buffered, reviewed, and only sent after settle;
 *  - eligible final = assistant message_end with a normal stopReason and no
 *    toolCall, latest wins.
 */

export type StreamFrame =
  | { type: "status"; text: string; at: number }
  | { type: "final"; text: string; at: number }
  | { type: "done"; at: number; reason: "settled" | "aborted" | "error" };

/** Deterministic tool→status mapping. No model text is involved. */
const STATUS_BY_TOOL: Record<string, string> = {
  order_query: "正在查询订单",
  knowledge_search: "正在检索服务政策",
  ticket_query: "正在查询工单",
  refund_evaluate: "正在评估退款条件",
  risk_check: "正在做风险校验",
  refund_confirm: "正在提交退款",
  ticket_create: "正在创建工单",
};

export function statusForTool(toolName: string): string {
  return STATUS_BY_TOOL[toolName] ?? "正在处理你的请求";
}

export function extractMessageText(message: unknown): string {
  const content = (message as { content?: unknown })?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) =>
      block && typeof block === "object" && (block as { type?: string }).type === "text"
        ? String((block as { text?: unknown }).text ?? "")
        : "",
    )
    .join("");
}

/**
 * An eligible final candidate is an assistant message that finished normally
 * and contains no tool call (a tool-call narration is not a final answer).
 */
export function isEligibleFinal(message: unknown): boolean {
  const m = message as { role?: string; stopReason?: string; content?: unknown };
  if (m?.role !== "assistant") return false;
  if (m.stopReason !== "stop") return false;
  if (Array.isArray(m.content) && m.content.some((b) => (b as { type?: string })?.type === "toolCall")) {
    return false;
  }
  return extractMessageText(message).trim().length > 0;
}

/**
 * Holds the latest eligible final. Plan v2 §6.5: "latest wins"; when no legal
 * candidate exists the caller must emit a deterministic safe fallback.
 */
export class RunOutputBuffer {
  private latest: string | undefined;

  record(message: unknown): boolean {
    if (!isEligibleFinal(message)) return false;
    this.latest = extractMessageText(message);
    return true;
  }

  peek(): string | undefined {
    return this.latest;
  }

  take(): string | undefined {
    const value = this.latest;
    this.latest = undefined;
    return value;
  }

  reset(): void {
    this.latest = undefined;
  }
}

export const SAFE_FALLBACK_FINAL = "抱歉，我这边暂时无法给出答复，请稍后再试或转人工客服。";

/** Minimal SSE writer: one `event:`/`data:` pair per frame. */
export function formatSseFrame(frame: StreamFrame): string {
  return `event: ${frame.type}\ndata: ${JSON.stringify(frame)}\n\n`;
}
