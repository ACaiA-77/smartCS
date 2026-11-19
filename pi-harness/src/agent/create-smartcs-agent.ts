/**
 * Personal Copilot Main Agent factory.
 *
 * Wiring:
 *  - explicit cwd / agentDir / sessionDir      (never ~/.pi discovery)
 *  - explicit Copilot system prompt            (override pi coding persona)
 *  - tool whitelist, built-ins off             (no bash/read/write/edit)
 *  - extension-only hooks for tool_call/result (subscribe cannot see them)
 *  - compliance replacement on message_end
 */

import { mkdirSync } from "node:fs";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  type AgentSession,
  type ExtensionFactory,
} from "@earendil-works/pi-coding-agent";
import { registerFauxProvider, type FauxProviderRegistration } from "@earendil-works/pi-ai/compat";
import { loadLlmConfigFromPythonEnv, resolveProviderMode, resolveSmartCsPaths, type SmartCsPaths } from "../config/env.js";
import { copilotPromptFor } from "./prompt/copilot.js";
import { createFakeTools, SMARTCS_TOOL_WHITELIST } from "./tools/fake-tools.js";
import { createSkillLoadTool, resolveSkillsEnabled, SKILL_LOAD_TOOL, skillsDir } from "./skills.js";
import {
  createKnowledgeMcpExtension,
  KNOWLEDGE_HTTP_TOOL,
  KNOWLEDGE_MCP_TOOL,
  resolveKnowledgeTransport,
} from "./mcp/knowledge-mcp.js";
import {
  createCopilotReadTools,
  createCopilotWriteTools,
  COPILOT_READ_TOOL_NAMES,
  COPILOT_WRITE_TOOL_NAMES,
} from "./tools/copilot-tools.js";
import { assertWriteModeWired, resolveWriteMode, writeToolsEnabled, type WriteMode } from "./write-mode.js";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { ShadowPlanStore } from "../db/shadow-plans.js";
import { TurnContext } from "../business/turn-context.js";
import { PythonInternalClient } from "../business/python-client.js";
import { createAuditExtension, createAuditSink, type AuditSink } from "./extensions/audit.js";
import { ATTR, startSpanFromTraceparent } from "../tracing/spans.js";
import type { AuditQueue } from "../tracing/audit-queue.js";
import { createComplianceExtension, type ComplianceHook, type ComplianceOptions } from "./extensions/compliance.js";
import {
  createContextInjectionExtension,
  SnapshotHolder,
  type ComplianceReviewer,
} from "./extensions/context-injection.js";

export const SMARTCS_PROVIDER_ID = "smartcs-openai-compat";

export const DEFAULT_OPENAI_COMPAT = {
  supportsDeveloperRole: false,
  supportsStore: false,
  maxTokensField: "max_tokens",
  supportsReasoningEffort: false,
} as const;

export interface CreateSmartCsAgentOptions {
  /** Session id; used verbatim as the Pi session id. */
  sessionId?: string;
  /** Explicit paths; defaults to env or pi-harness/.runtime. */
  paths?: Partial<SmartCsPaths>;
  /** Force a provider mode instead of auto-detecting from python-impl/.env. */
  provider?: "openai" | "faux";
  /** Pre-registered faux provider (tests own its lifecycle). */
  faux?: FauxProviderRegistration;
  /**
   * Which tool layer to mount:
   *  - "business" (default): the real Copilot READ/WRITE shells calling Python Runtime
   *  - "fake": offline hard-coded doubles for SDK-level tests
   */
  toolMode?: "business" | "fake";
  /** Python Runtime client; required for toolMode "business". */
  pythonClient?: PythonInternalClient;
  /** Per-turn identity holder shared with the pipeline. */
  turnContext?: TurnContext;
  /** Per-turn snapshot holder; prefetched by the pipeline. */
  snapshotHolder?: SnapshotHolder;
  /** Authoritative compliance reviewer (Python rule mask + optional LLM). */
  complianceReviewer?: ComplianceReviewer;
  /** Pi compaction settings. */
  compaction?: Record<string, unknown>;
  /** Write mode; defaults to SMARTCS_WRITE_MODE (off). */
  writeMode?: WriteMode;
  /** Shadow plan store; required when writeMode is "shadow". */
  shadowPlanStore?: ShadowPlanStore;
  /** Receipt store; required in live mode for operation-id recovery. */
  receiptStore?: import("../db/receipts.js").ReceiptStore;
  /** Turn counter + last user message for the shadow plan record. */
  shadowTurnContext?: { turnIndex: () => number; lastUserMessage: () => string | undefined };
  /** Whitelist override. Defaults to the mounted tool layer. */
  tools?: string[];
  /** Extra tool definitions appended after the standard set. */
  additionalTools?: ToolDefinition[];
  /** Suppression mode passed through to the SDK. */
  noTools?: "all" | "builtin";
  /** Session manager override; when omitted a file-backed one is created. */
  sessionManager?: SessionManager;
  /** Compliance hook policy override. */
  compliance?: ComplianceOptions;
  /** Audit sink override (tests inspect records). */
  auditSink?: AuditSink;
  /** Durable audit delivery queue. */
  auditQueue?: AuditQueue;
  /** Extra inline extensions appended after the built-in ones. */
  extraExtensions?: ExtensionFactory[];
  /** Model runtime override. */
  modelRuntime?: ModelRuntime;
  /** Skip SessionManager creation entirely (in-memory tests). */
  inMemorySession?: boolean;
  /** Settings overrides for the in-memory SettingsManager. */
  settings?: Record<string, unknown>;
}

export interface SmartCsAgentHandle {
  session: AgentSession;
  sessionManager: SessionManager;
  modelRuntime: ModelRuntime;
  resourceLoader: DefaultResourceLoader;
  audit: AuditSink;
  compliance: ComplianceHook;
  turnContext: TurnContext;
  snapshotHolder: SnapshotHolder;
  toolMode: "business" | "fake";
  writeMode: WriteMode;
  providerMode: "openai" | "faux";
  modelId: string;
  paths: SmartCsPaths;
  dispose: () => void;
}

async function buildModelRuntime(
  mode: "openai" | "faux",
  faux: FauxProviderRegistration | undefined,
  provided: ModelRuntime | undefined,
): Promise<{ runtime: ModelRuntime; modelId: string }> {
  if (provided) {
    if (mode === "faux" && faux) return { runtime: provided, modelId: faux.getModel().id };
    const cfg = loadLlmConfigFromPythonEnv();
    return { runtime: provided, modelId: cfg?.model ?? process.env.MODEL_NAME ?? "unknown" };
  }

  if (mode === "faux") {
    if (!faux) throw new Error("faux provider requested but no registration was supplied");
    const runtime = await ModelRuntime.create({ modelsPath: null, allowModelNetwork: false });
    const definition = faux.models[0]!;
    runtime.registerProvider(definition.provider, {
      baseUrl: definition.baseUrl,
      apiKey: "faux-key",
      api: faux.api,
      models: faux.models.map((m) => ({
        id: m.id,
        name: m.name,
        api: m.api,
        reasoning: m.reasoning,
        input: m.input,
        cost: m.cost,
        contextWindow: m.contextWindow,
        maxTokens: m.maxTokens,
        baseUrl: m.baseUrl,
      })),
    });
    return { runtime, modelId: definition.id };
  }

  const cfg = loadLlmConfigFromPythonEnv();
  if (!cfg) throw new Error("no usable LLM config found in python-impl/.env");
  const runtime = await ModelRuntime.create({ modelsPath: null, allowModelNetwork: false });
  runtime.registerProvider(SMARTCS_PROVIDER_ID, {
    name: "Copilot OpenAI-compatible endpoint",
    baseUrl: cfg.baseUrl,
    apiKey: cfg.apiKey,
    api: "openai-completions",
    models: [
      {
        id: cfg.model,
        name: cfg.model,
        api: "openai-completions",
        baseUrl: cfg.baseUrl,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128_000,
        maxTokens: 8_192,
        compat: { ...DEFAULT_OPENAI_COMPAT },
      },
    ],
  });
  return { runtime, modelId: cfg.model };
}

export async function createSmartCsAgent(options: CreateSmartCsAgentOptions = {}): Promise<SmartCsAgentHandle> {
  const paths = resolveSmartCsPaths(options.paths);
  mkdirSync(paths.runtimeCwd, { recursive: true });
  mkdirSync(paths.sessionDir, { recursive: true });
  mkdirSync(paths.agentDir, { recursive: true });

  const providerMode = options.provider ?? resolveProviderMode();
  const toolMode = options.toolMode ?? "business";
  const turnContext = options.turnContext ?? new TurnContext();
  const writeMode = options.writeMode ?? resolveWriteMode();
  assertWriteModeWired(writeMode, { durableOperationLog: Boolean(options.receiptStore) });
  const { runtime: modelRuntime, modelId } = await buildModelRuntime(providerMode, options.faux, options.modelRuntime);
  const model = modelRuntime.getModel(
    providerMode === "faux" ? options.faux!.models[0]!.provider : SMARTCS_PROVIDER_ID,
    modelId,
  );
  if (!model) throw new Error(`model not resolvable: ${modelId}`);

  const audit = options.auditSink ?? createAuditSink({ queue: options.auditQueue });
  const snapshotHolder = options.snapshotHolder ?? new SnapshotHolder();

  const businessClient =
    options.toolMode === "fake" && !options.pythonClient ? undefined : options.pythonClient ?? new PythonInternalClient();
  const builtReviewer: ComplianceReviewer | undefined = businessClient
    ? async (text: string) => {
        const identity = turnContext.peek();
        if (!identity) return undefined;
        const span = startSpanFromTraceparent("smartcs.compliance.review", identity.traceparent, {
          [ATTR.sessionId]: identity.sessionId,
          [ATTR.clientRequestId]: identity.clientRequestId,
          ...(identity.agentRunId ? { [ATTR.agentRunId]: identity.agentRunId } : {}),
        });
        try {
          const verdict = await businessClient.reviewCompliance({ identity, text });
          span.setAttribute("smartcs.compliance.verdict", verdict.verdict);
          return { verdict: verdict.verdict, replacement: verdict.replacement };
        } finally {
          span.end();
        }
      }
    : undefined;
  const reviewer =
    options.complianceReviewer ??
    (options.compliance?.reviewer as ComplianceReviewer | undefined) ??
    builtReviewer;
  const { extension: complianceExtension, hook: compliance } = createComplianceExtension({
    ...options.compliance,
    reviewer,
  });

  const settingsManager = SettingsManager.inMemory(
    (options.settings as never) ?? {
      compaction: {
        enabled: true,
        reserveTokens: 8_192,
        keepRecentTokens: 16_384,
        ...(options.compaction ?? {}),
      },
    },
  );
  const knowledgeTransport = toolMode === "fake" ? "http" : resolveKnowledgeTransport();
  const knowledgeToolName = knowledgeTransport === "mcp" ? KNOWLEDGE_MCP_TOOL : KNOWLEDGE_HTTP_TOOL;
  const shadowStore = options.shadowPlanStore;
  const writeToolsMounted =
    toolMode !== "fake" && (writeMode === "live" || (writeMode === "shadow" && Boolean(shadowStore)));
  const extensionFactories: ExtensionFactory[] = [
    createAuditExtension(audit, turnContext),
    createContextInjectionExtension(snapshotHolder),
    complianceExtension,
    ...(knowledgeTransport === "mcp" ? [createKnowledgeMcpExtension()] : []),
    ...(options.extraExtensions ?? []),
  ];

  const skillsEnabled = resolveSkillsEnabled();
  const resourceLoader: DefaultResourceLoader = new DefaultResourceLoader({
    cwd: paths.runtimeCwd,
    agentDir: paths.agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    ...(skillsEnabled ? { additionalSkillPaths: [skillsDir()] } : {}),
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPrompt: copilotPromptFor(knowledgeToolName, { writeTools: writeToolsMounted }),
    ...(skillsEnabled
      ? {
          systemPromptOverride: (base: string | undefined) => {
            const loaded = resourceLoader.getSkills().skills;
            if (loaded.length === 0) return base;
            const section = [
              "## 可用技能（Skills）",
              "以下是工作台已注册的知识技能；需要细节时用 skill_load 取正文。技能只说明操作流程，任何写操作权限以运行时授权结果为准。",
              ...loaded.map((skill) => `- ${skill.name}: ${skill.description}`),
            ].join("\n");
            return base ? `${base}\n\n${section}` : section;
          },
        }
      : {}),
    extensionFactories,
  });
  await resourceLoader.reload();

  const sessionManager =
    options.sessionManager ??
    (options.inMemorySession
      ? SessionManager.inMemory(paths.runtimeCwd, options.sessionId ? { id: options.sessionId } : undefined)
      : SessionManager.create(paths.runtimeCwd, paths.sessionDir, options.sessionId ? { id: options.sessionId } : undefined));

  let customTools;
  if (toolMode === "fake") {
    customTools = createFakeTools();
  } else {
    const client = businessClient ?? new PythonInternalClient();
    customTools = createCopilotReadTools({ client, turnContext });
    if (knowledgeTransport === "mcp") {
      customTools = customTools.filter((tool) => tool.name !== KNOWLEDGE_HTTP_TOOL);
    }
    if (writeToolsMounted) {
      customTools = [
        ...customTools,
        ...createCopilotWriteTools({
          mode: writeMode,
          store: shadowStore,
          turnContext,
          client: businessClient,
          receipts: options.receiptStore,
          turnIndex:
            options.shadowTurnContext?.turnIndex ?? (() => userTurns(sessionManager) - 1),
          lastUserMessage:
            options.shadowTurnContext?.lastUserMessage ?? (() => lastUserText(sessionManager)),
        }),
      ];
    }
  }
  if (skillsEnabled) {
    const loadedSkills = resourceLoader.getSkills().skills;
    if (loadedSkills.length > 0) {
      customTools = [...customTools, createSkillLoadTool(loadedSkills)];
    }
  }

  const readToolNames =
    knowledgeTransport === "mcp"
      ? COPILOT_READ_TOOL_NAMES.filter((name) => name !== KNOWLEDGE_HTTP_TOOL)
      : [...COPILOT_READ_TOOL_NAMES];
  const writeToolNames = COPILOT_WRITE_TOOL_NAMES;
  if (options.additionalTools?.length) {
    customTools = [...customTools, ...options.additionalTools];
  }

  const defaultWhitelist: string[] =
    toolMode === "fake"
      ? [...SMARTCS_TOOL_WHITELIST]
      : writeToolsMounted
        ? [...readToolNames, ...writeToolNames]
        : [...readToolNames];
  if (toolMode !== "fake" && knowledgeTransport === "mcp") {
    defaultWhitelist.push(KNOWLEDGE_MCP_TOOL);
  }
  if (skillsEnabled) {
    defaultWhitelist.push(SKILL_LOAD_TOOL);
  }
  if (options.additionalTools?.length && options.tools === undefined) {
    defaultWhitelist.push(...options.additionalTools.map((tool) => tool.name));
  }

  const { session } = await createAgentSession({
    cwd: paths.runtimeCwd,
    agentDir: paths.agentDir,
    model,
    modelRuntime,
    resourceLoader,
    settingsManager,
    sessionManager,
    thinkingLevel: "off",
    tools: options.tools ?? defaultWhitelist,
    noTools: options.noTools,
    customTools,
  });

  if (knowledgeTransport === "mcp") {
    await session.bindExtensions({ mode: "print" });
  }

  return {
    session,
    sessionManager,
    modelRuntime,
    resourceLoader,
    audit,
    compliance,
    turnContext,
    snapshotHolder,
    toolMode,
    writeMode,
    providerMode,
    modelId,
    paths,
    dispose: () => session.dispose(),
  };
}

/** Count real user turns (the injected snapshot is a custom_message, not a message). */
function userTurns(sessionManager: SessionManager): number {
  return sessionManager
    .getEntries()
    .filter(
      (entry) =>
        entry.type === "message" && (entry as { message?: { role?: string } }).message?.role === "user",
    ).length;
}

function lastUserText(sessionManager: SessionManager): string | undefined {
  const users = sessionManager
    .getEntries()
    .filter(
      (entry) =>
        entry.type === "message" && (entry as { message?: { role?: string } }).message?.role === "user",
    );
  const last = users.at(-1) as { message?: { content?: unknown } } | undefined;
  const content = last?.message?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return undefined;
  return content.map((block) => (block as { text?: string }).text ?? "").join("");
}

/** Register a faux provider for offline tests. Caller must unregister. */
export function createFauxProvider(): FauxProviderRegistration {
  return registerFauxProvider({ provider: "faux", api: "faux", models: [{ id: "faux-1", name: "Faux 1" }] });
}
