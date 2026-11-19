/**
 * Copilot tool shells (`SMARTCS_PROFILE=copilot`).
 *
 * Transport only, exactly like `business-tools.ts`: each shell posts to the
 * runtime's `/internal/tools/execute` and hands the answer back. Retry, timeout,
 * authorization, the two-phase `pending_action` snapshot and the execution
 * ledger all live in Python.
 *
 * ── Model-visible schema rule (Phase 10 §③, carried into the Copilot) ──
 *
 * The declared parameters are BUSINESS parameters only. Identity (`user_id`,
 * `account_id`, `session_id`), authorization (`confirmed`, `approval_id`),
 * idempotency (`idempotency_key`, `uuid_key`, `operation_id`) and tracing
 * (`client_request_id`, `request_payload_hash`) fields belong to the runtime;
 * the Python side force-binds or generates them. Declaring them here would
 * suggest the model is responsible for something it cannot influence.
 *
 * ── Write modes ──
 *
 * `shadow` records the decision in `shadow_write_plan` and returns a canned
 * acknowledgement — no HTTP request, so no side effect is possible.
 * `live` calls the runtime through the SAME shell; the runtime decides whether
 * the write happens. `copilot_action_prepare` is read-class on the Python side
 * (it records intent only), so it calls the runtime directly in live mode and
 * surfaces the returned `pending_action_id`.
 */

import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import { PythonInternalError, type PythonInternalClient } from "../../business/python-client.js";
import type { TurnContext } from "../../business/turn-context.js";
import type { ShadowPlanStore } from "../../db/shadow-plans.js";
import type { ReceiptStore } from "../../db/receipts.js";
import { WriteToolNotPermittedError, type WriteMode } from "../write-mode.js";
import { executeLiveWrite } from "./live-write-executor.js";

export const COPILOT_READ_TOOL_NAMES = [
  "knowledge_search",
  "vault_search",
  "vault_read_note",
  "vault_backlinks",
  "wiki_lint",
  "onto_query",
  "mail_search",
  "mail_read_thread",
  "gh_list_notifications",
  "gh_get_issue_or_pr",
  "gh_search_code",
  "feishu_list_chats",
  "feishu_read_messages",
  "feishu_read_doc",
] as const;

export type CopilotReadToolName = (typeof COPILOT_READ_TOOL_NAMES)[number];

export const COPILOT_WRITE_TOOL_NAMES = [
  "vault_append_daily",
  "vault_propose_edit",
  "wiki_compile_doc",
  "onto_upsert",
  "mail_save_draft",
  "copilot_action_prepare",
  "copilot_action_confirm",
] as const;

export type CopilotWriteToolName = (typeof COPILOT_WRITE_TOOL_NAMES)[number];

export interface CopilotReadDeps {
  client: PythonInternalClient;
  turnContext: TurnContext;
}

export interface CopilotWriteDeps {
  mode: WriteMode;
  /** Required in shadow mode; live mode never records a plan. */
  store?: ShadowPlanStore;
  turnContext: TurnContext;
  /** Required in live mode: the runtime client and the durable operation log. */
  client?: PythonInternalClient;
  receipts?: ReceiptStore;
  turnIndex?: () => number;
  lastUserMessage?: () => string | undefined;
}

export const SHADOW_CANNED_RESULT: Record<CopilotWriteToolName, string> = {
  vault_append_daily: "[SHADOW] 日记追加已记录为计划，未执行",
  vault_propose_edit: "[SHADOW] 笔记修改提案已记录为计划，未执行",
  wiki_compile_doc: "[SHADOW] Wiki 编译已记录为计划，未执行",
  onto_upsert: "[SHADOW] 本体实体写入已记录为计划，未执行",
  mail_save_draft: "[SHADOW] 邮件草稿已记录为计划，未执行",
  copilot_action_prepare: "[SHADOW] 外部操作确认已记录为计划，未执行",
  copilot_action_confirm: "[SHADOW] 外部操作确认已记录为计划，未执行",
};

/** A tool failure becomes a thrown Error the model can read and react to. */
function toToolError(error: unknown, tool: string): Error {
  if (error instanceof PythonInternalError) {
    return new Error(`工具 ${tool} 调用失败（${error.status}）：${error.detail}`);
  }
  return new Error(`工具 ${tool} 调用失败：${error instanceof Error ? error.message : String(error)}`);
}

function readShell(
  deps: CopilotReadDeps,
  definition: {
    name: CopilotReadToolName;
    label: string;
    description: string;
    parameters: ReturnType<typeof Type.Object>;
  },
): ToolDefinition {
  return defineTool({
    name: definition.name,
    label: definition.label,
    description: definition.description,
    parameters: definition.parameters,
    async execute(toolCallId, params, signal): Promise<AgentToolResult<unknown>> {
      const identity = deps.turnContext.require();
      try {
        const response = await deps.client.executeTool({
          tool: definition.name,
          arguments: params as Record<string, unknown>,
          identity,
          toolCallId,
          signal,
        });
        return {
          content: [{ type: "text" as const, text: response.content }],
          details: response.details,
        };
      } catch (error) {
        throw toToolError(error, definition.name);
      }
    },
  }) as unknown as ToolDefinition;
}

/**
 * Deterministic placeholder id for shadow-mode `copilot_action_prepare`.
 *
 * Live mode returns the runtime's real id. Shadow mode writes nothing, so the
 * id is derived from the verified identity plus the action, which makes it
 * stable across runs and therefore comparable.
 */
export function shadowCopilotPendingActionId(parts: {
  sessionId: string;
  clientRequestId: string;
  tool: string;
}): string {
  const input = `${parts.sessionId}|${parts.clientRequestId}|${parts.tool}`;
  let hash = 0;
  for (let index = 0; index < input.length; index += 1) {
    hash = (hash * 31 + input.charCodeAt(index)) >>> 0;
  }
  return `pending-shadow-${hash.toString(16).padStart(8, "0")}`;
}

async function intercept(
  deps: CopilotWriteDeps,
  toolName: CopilotWriteToolName,
  params: Record<string, unknown>,
  signal?: AbortSignal,
  toolCallId?: string,
): Promise<AgentToolResult<unknown>> {
  if (deps.mode === "live") {
    if (!deps.client || !deps.receipts) {
      throw new Error(`live write mode is not fully wired for ${toolName}`);
    }
    const identity = deps.turnContext.require();
    if (toolName === "copilot_action_prepare") {
      // Read-class on the Python side: records intent and returns the real
      // pending action. It must NOT go through ledger reconciliation.
      try {
        return await callPrepare(deps.client, identity, params, toolCallId, signal);
      } catch (error) {
        throw toToolError(error, toolName);
      }
    }
    return executeLiveWrite(
      { client: deps.client, receipts: deps.receipts },
      identity,
      toolName,
      params,
      signal,
      toolCallId,
    );
  }

  if (deps.mode !== "shadow") throw new WriteToolNotPermittedError(toolName);
  if (!deps.store) throw new Error(`shadow mode requires a plan store for ${toolName}`);

  const identity = deps.turnContext.require();
  const planId = await deps.store.record({
    sessionId: identity.sessionId,
    clientRequestId: identity.clientRequestId,
    toolName,
    arguments: params,
    turnIndex: deps.turnIndex?.() ?? 0,
    userMessageExcerpt: deps.lastUserMessage?.() ?? "",
  });

  const pendingActionId =
    toolName === "copilot_action_prepare"
      ? shadowCopilotPendingActionId({
          sessionId: identity.sessionId,
          clientRequestId: identity.clientRequestId,
          tool: String(params.tool ?? ""),
        })
      : undefined;

  const text = pendingActionId
    ? `${SHADOW_CANNED_RESULT[toolName]}\npending_action_id=${pendingActionId}`
    : SHADOW_CANNED_RESULT[toolName];

  return {
    content: [{ type: "text" as const, text }],
    details: {
      shadow: true,
      executed: false,
      planId,
      tool: toolName,
      ...(pendingActionId ? { pending_action_id: pendingActionId } : {}),
      // Program-only metadata; never enters the model's context.
      planArguments: params,
    },
  };
}

async function callPrepare(
  client: PythonInternalClient,
  identity: ReturnType<TurnContext["require"]>,
  params: Record<string, unknown>,
  toolCallId?: string,
  signal?: AbortSignal,
): Promise<AgentToolResult<unknown>> {
  const response = await client.executeTool({
    tool: "copilot_action_prepare",
    arguments: params,
    identity,
    toolCallId,
    signal,
  });
  return {
    content: [{ type: "text" as const, text: response.content }],
    details: response.details,
  };
}

export function createCopilotReadTools(deps: CopilotReadDeps): ToolDefinition[] {
  return [
    // mcp_server.py / business-tools.ts: knowledge_search. With the MCP
    // transport this shell is filtered out by the caller, exactly as in the
    // e-commerce profile.
    readShell(deps, {
      name: "knowledge_search",
      label: "知识库检索",
      description: "检索知识库（Wiki / 文档），返回相关片段",
      parameters: Type.Object(
        {
          query: Type.String({ description: "搜索查询" }),
          top_k: Type.Optional(Type.Integer({ description: "返回数量", default: 3 })),
          domain: Type.Optional(Type.String({ description: "可选知识域" })),
          domains: Type.Optional(Type.Array(Type.String())),
        },
        { additionalProperties: false },
      ),
    }),
    // tool_runtime/copilot_tools.py: vault_search
    readShell(deps, {
      name: "vault_search",
      label: "Obsidian 检索",
      description: "按标题、标签或正文搜索 Obsidian 笔记",
      parameters: Type.Object(
        {
          query: Type.String({ description: "搜索查询" }),
          zone: Type.Optional(
            Type.String({ enum: ["wiki", "raw", "notes", "daily", "source"], description: "限定区域" }),
          ),
          limit: Type.Optional(Type.Integer({ description: "返回数量", default: 10 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "vault_read_note",
      label: "读取笔记",
      description: "读取一篇 Obsidian 笔记及其链接与标签",
      parameters: Type.Object(
        { path: Type.String({ description: "笔记相对路径" }) },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "vault_backlinks",
      label: "反链查询",
      description: "列出链接到某个 wikilink 目标的笔记",
      parameters: Type.Object(
        { target: Type.String({ description: "wikilink 目标" }) },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "wiki_lint",
      label: "Wiki 健康检查",
      description: "报告断链、孤儿页面与未收录索引的 wiki 页面",
      parameters: Type.Object({}, { additionalProperties: false }),
    }),
    readShell(deps, {
      name: "onto_query",
      label: "本体查询",
      description: "按实体类型与自由文本查询本体实体",
      parameters: Type.Object(
        {
          entity_type: Type.Optional(Type.String({ description: "实体类型" })),
          text: Type.Optional(Type.String({ description: "自由文本" })),
          limit: Type.Optional(Type.Integer({ description: "返回数量", default: 20 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "mail_search",
      label: "邮件检索",
      description: "搜索已配置的邮箱账号",
      parameters: Type.Object(
        {
          account: Type.String({ description: "邮箱账号名，如 163 或 gmail" }),
          query: Type.String({ description: "搜索查询" }),
          folder: Type.Optional(Type.String({ description: "文件夹", default: "INBOX" })),
          limit: Type.Optional(Type.Integer({ description: "返回数量", default: 10 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "mail_read_thread",
      label: "读取邮件会话",
      description: "读取一封邮件的完整会话",
      parameters: Type.Object(
        {
          account: Type.String({ description: "邮箱账号名" }),
          thread_id: Type.String({ description: "会话/邮件 ID" }),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "gh_list_notifications",
      label: "GitHub 通知",
      description: "列出 GitHub 通知",
      parameters: Type.Object(
        {
          all_notifications: Type.Optional(Type.Boolean({ description: "包含已读" })),
          participating: Type.Optional(Type.Boolean({ description: "仅参与过的" })),
          limit: Type.Optional(Type.Integer({ description: "返回数量", default: 20 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "gh_get_issue_or_pr",
      label: "读取 Issue/PR",
      description: "获取一个 GitHub issue 或 pull request",
      parameters: Type.Object(
        {
          owner: Type.String({ description: "仓库所有者" }),
          repo: Type.String({ description: "仓库名" }),
          number: Type.Integer({ description: "编号" }),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "gh_search_code",
      label: "GitHub 代码检索",
      description: "搜索 GitHub 代码，可限定仓库",
      parameters: Type.Object(
        {
          query: Type.String({ description: "搜索查询" }),
          repo: Type.Optional(Type.String({ description: "owner/name" })),
          limit: Type.Optional(Type.Integer({ description: "返回数量", default: 10 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "feishu_list_chats",
      label: "飞书会话列表",
      description: "列出租户应用可见的飞书会话",
      parameters: Type.Object(
        { page_size: Type.Optional(Type.Integer({ description: "返回数量", default: 20 })) },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "feishu_read_messages",
      label: "读取飞书消息",
      description: "读取某个飞书会话或话题容器中的消息",
      parameters: Type.Object(
        {
          container_id: Type.String({ description: "容器 ID（chat_id 等）" }),
          container_id_type: Type.Optional(Type.String({ description: "容器类型", default: "chat" })),
          page_size: Type.Optional(Type.Integer({ description: "返回数量", default: 20 })),
        },
        { additionalProperties: false },
      ),
    }),
    readShell(deps, {
      name: "feishu_read_doc",
      label: "读取飞书文档",
      description: "读取飞书 Docx 文档的纯文本内容",
      parameters: Type.Object(
        { document_id: Type.String({ description: "文档 ID" }) },
        { additionalProperties: false },
      ),
    }),
  ];
}

function writeTool(
  deps: CopilotWriteDeps,
  definition: {
    name: CopilotWriteToolName;
    label: string;
    description: string;
    parameters: ReturnType<typeof Type.Object>;
  },
): ToolDefinition {
  return defineTool({
    name: definition.name,
    label: definition.label,
    description: definition.description,
    parameters: definition.parameters,
    async execute(toolCallId, params, signal) {
      return intercept(deps, definition.name, params as Record<string, unknown>, signal, toolCallId);
    },
  }) as unknown as ToolDefinition;
}

export function createCopilotWriteTools(deps: CopilotWriteDeps): ToolDefinition[] {
  return [
    // Local writes: vault / ontology / wiki / draft. Same-turn consent is
    // enough; the runtime still owns the authorization decision.
    writeTool(deps, {
      name: "vault_append_daily",
      label: "追加日记",
      description: "向 daily/YYYY-MM-DD.md 追加一条记录",
      parameters: Type.Object(
        {
          entry: Type.String({ description: "要追加的内容" }),
          date_str: Type.Optional(Type.String({ description: "日期 YYYY-MM-DD" })),
        },
        { additionalProperties: false },
      ),
    }),
    writeTool(deps, {
      name: "vault_propose_edit",
      label: "笔记修改提案",
      description: "对 notes/ 下的笔记提出修改建议（只提案，不直接覆盖）",
      parameters: Type.Object(
        {
          target_rel_path: Type.String({ description: "目标笔记相对路径" }),
          rationale: Type.String({ description: "修改理由" }),
          proposed_content: Type.String({ description: "建议的完整内容" }),
        },
        { additionalProperties: false },
      ),
    }),
    writeTool(deps, {
      name: "wiki_compile_doc",
      label: "编译 Wiki 文档",
      description: "归档一个来源并编译/更新其 wiki 页面",
      parameters: Type.Object(
        {
          source_uri: Type.String({ description: "来源 URI" }),
          title: Type.String({ description: "标题" }),
          raw_content: Type.String({ description: "原文内容" }),
          summary_markdown: Type.String({ description: "摘要 Markdown" }),
          slug: Type.Optional(Type.String({ description: "页面 slug" })),
          related_entities: Type.Optional(
            Type.Array(Type.Object({}, { additionalProperties: true })),
          ),
        },
        { additionalProperties: false },
      ),
    }),
    writeTool(deps, {
      name: "onto_upsert",
      label: "写入本体实体",
      description: "插入或更新一个本体实体",
      parameters: Type.Object(
        {
          entity_type: Type.String({ description: "实体类型" }),
          name: Type.String({ description: "实体名称" }),
          canonical_key: Type.Optional(Type.String({ description: "规范键" })),
          attrs: Type.Optional(Type.Object({}, { additionalProperties: true })),
        },
        { additionalProperties: false },
      ),
    }),
    writeTool(deps, {
      name: "mail_save_draft",
      label: "保存邮件草稿",
      description: "创建一封邮件草稿（不发送）",
      parameters: Type.Object(
        {
          account: Type.String({ description: "邮箱账号名" }),
          to: Type.Union([Type.String(), Type.Array(Type.String())], { description: "收件人" }),
          subject: Type.String({ description: "主题" }),
          body: Type.String({ description: "正文" }),
          cc: Type.Optional(Type.Union([Type.String(), Type.Array(Type.String())])),
          reply_to_message_id: Type.Optional(Type.String({ description: "回复的邮件 ID" })),
        },
        { additionalProperties: false },
      ),
    }),
    // External side effects: two-phase, frozen snapshot.
    writeTool(deps, {
      name: "copilot_action_prepare",
      label: "准备外部操作",
      description:
        "准备一个外部操作（发邮件、建 Issue、发评论、发飞书消息），返回待确认动作 ID；不会执行副作用，必须等待用户明确确认后再调用 copilot_action_confirm",
      parameters: Type.Object(
        {
          tool: Type.String({
            description: "外部操作名称：mail_send / gh_create_issue / gh_post_comment / feishu_send_message",
          }),
          arguments: Type.Optional(
            Type.Object({}, { additionalProperties: true, description: "该操作的业务参数" }),
          ),
        },
        { additionalProperties: false },
      ),
    }),
    writeTool(deps, {
      name: "copilot_action_confirm",
      label: "确认外部操作",
      description: "在用户明确确认后，执行 copilot_action_prepare 冻结的外部操作",
      parameters: Type.Object(
        { pending_action_id: Type.String({ description: "准备阶段返回的待确认动作 ID" }) },
        { additionalProperties: false },
      ),
    }),
  ];
}
