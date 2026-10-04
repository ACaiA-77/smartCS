/**
 * SmartCS Main Agent factory (Phase 0 spike).
 *
 * Wiring follows plan v2 §8 (server-side de-coding-assistant) and §4.2:
 *  - explicit cwd / agentDir / sessionDir      (never ~/.pi discovery)
 *  - explicit system prompt                    (override pi coding persona)
 *  - tool whitelist, built-ins off             (no bash/read/write/edit)
 *  - extension-only hooks for tool_call/result (subscribe cannot see them)
 *  - compliance replacement on message_end     (A2)
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
import { systemPromptFor } from "./prompt/customer-service.js";
import { createFakeTools, SMARTCS_TOOL_WHITELIST } from "./tools/fake-tools.js";
import { createSkillLoadTool, resolveSkillsEnabled, SKILL_LOAD_TOOL, skillsDir } from "./skills.js";
import {
  createKnowledgeMcpExtension,
  KNOWLEDGE_HTTP_TOOL,
  KNOWLEDGE_MCP_TOOL,
  resolveKnowledgeTransport,
} from "./mcp/knowledge-mcp.js";
import { createBusinessReadTools, READ_TOOL_NAMES } from "./tools/business-tools.js";
import {
  createShadowWriteTools,
  shadowPendingActionId,
  SHADOW_WRITE_TOOL_NAMES,
} from "./tools/shadow-write-tools.js";
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

/**
 * OpenAI-compatible compat switches. Moonshot/DeepSeek-style endpoints reject
 * the `developer` role and `store`, and expect `max_tokens`. Plan v2 Appendix A
 * lists these as conditional ("按需"); Phase 0 pins them explicitly so provider
 * behaviour does not depend on URL auto-detection.
 */
export const DEFAULT_OPENAI_COMPAT = {
  supportsDeveloperRole: false,
  supportsStore: false,
  maxTokensField: "max_tokens",
  supportsReasoningEffort: false,
} as const;

export interface CreateSmartCsAgentOptions {
  /** SmartCS session id; used verbatim as the Pi session id (plan v2 §5.2). */
  sessionId?: string;
  /** Explicit paths; defaults to env or pi-harness/.runtime. */
  paths?: Partial<SmartCsPaths>;
  /** Force a provider mode instead of auto-detecting from python-impl/.env. */
  provider?: "openai" | "faux";
  /** Pre-registered faux provider (tests own its lifecycle). */
  faux?: FauxProviderRegistration;
  /**
   * Which tool layer to mount:
   *  - "business" (default): the 5 real READ shells calling the Business Runtime
   *  - "fake": the Phase 0 hard-coded doubles, for SDK-level offline tests
   */
  toolMode?: "business" | "fake";
  /** Business Runtime client; required for toolMode "business". */
  pythonClient?: PythonInternalClient;
  /** Per-turn identity holder shared with the pipeline. */
  turnContext?: TurnContext;
  /** Per-turn business snapshot holder; prefetched by the pipeline. */
  snapshotHolder?: SnapshotHolder;
  /** Authoritative compliance reviewer (Python rule mask + optional LLM). */
  complianceReviewer?: ComplianceReviewer;
  /** Pi compaction settings; defaults to the Phase 3 baseline profile. */
  compaction?: Record<string, unknown>;
  /** Phase 4 write mode; defaults to SMARTCS_WRITE_MODE (off). */
  writeMode?: WriteMode;
  /** Shadow plan store; required when writeMode is "shadow". */
  shadowPlanStore?: ShadowPlanStore;
  /** Receipt store; required in live mode for operation-id recovery. */
  receiptStore?: import("../db/receipts.js").ReceiptStore;
  /** Turn counter + last user message for the shadow plan record. */
  shadowTurnContext?: { turnIndex: () => number; lastUserMessage: () => string | undefined };
  /** Whitelist override (A8 experiments). Defaults to the mounted tool layer. */
  tools?: string[];
  /**
   * Extra tool definitions appended after the standard set (measurement
   * harnesses, protocol probes). They join the whitelist automatically when the
   * caller has not pinned `tools`, so they are declared like any other tool.
   */
  additionalTools?: ToolDefinition[];
  /** Suppression mode passed through to the SDK (A8 experiments). */
  noTools?: "all" | "builtin";
  /** Session manager override; when omitted a file-backed one is created. */
  sessionManager?: SessionManager;
  /** Compliance hook policy override. */
  compliance?: ComplianceOptions;
  /** Audit sink override (tests inspect records). */
  auditSink?: AuditSink;
  /**
   * Phase 6: durable audit delivery. When present, tool events are queued and
   * shipped to /internal/audit by a background dispatcher the caller owns.
   */
  auditQueue?: AuditQueue;
  /** Extra inline extensions appended after the built-in two. */
  extraExtensions?: ExtensionFactory[];
  /** Model runtime override (must already know the selected provider). */
  modelRuntime?: ModelRuntime;
  /** Skip SessionManager creation entirely (in-memory tests). */
  inMemorySession?: boolean;
  /** Settings overrides (retry/compaction) for the in-memory SettingsManager. */
  settings?: Record<string, unknown>;
}

export interface SmartCsAgentHandle {
  session: AgentSession;
  sessionManager: SessionManager;
  modelRuntime: ModelRuntime;
  resourceLoader: DefaultResourceLoader;
  audit: AuditSink;
  compliance: ComplianceHook;
  /** Identity holder the pipeline publishes into before each prompt. */
  turnContext: TurnContext;
  /** Snapshot holder the pipeline fills before each prompt. */
  snapshotHolder: SnapshotHolder;
  toolMode: "business" | "fake";
  /** Effective Phase 4 write mode for this agent. */
  writeMode: WriteMode;
  providerMode: "openai" | "faux";
  modelId: string;
  paths: SmartCsPaths;
  /** Dispose the session. Does not unregister a caller-owned faux provider. */
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
    name: "SmartCS OpenAI-compatible endpoint",
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
  // `live` is a reviewed act: refuse to run rather than silently degrade into a
  // mode that cannot reconcile a dropped write response.
  assertWriteModeWired(writeMode, { durableOperationLog: Boolean(options.receiptStore) });
  const { runtime: modelRuntime, modelId } = await buildModelRuntime(providerMode, options.faux, options.modelRuntime);
  const model = modelRuntime.getModel(
    providerMode === "faux" ? options.faux!.models[0]!.provider : SMARTCS_PROVIDER_ID,
    modelId,
  );
  if (!model) throw new Error(`model not resolvable: ${modelId}`);

  const audit = options.auditSink ?? createAuditSink({ queue: options.auditQueue });
  const snapshotHolder = options.snapshotHolder ?? new SnapshotHolder();

  // A business-mode agent reviews every eligible final answer through the
  // runtime. Identity is read at call time from the turn context (tools and
  // compliance share the same per-turn identity), so an agent created outside a
  // chat request simply keeps the local rule behaviour.
  const businessClient =
    options.toolMode === "fake" && !options.pythonClient ? undefined : options.pythonClient ?? new PythonInternalClient();
  const builtReviewer: ComplianceReviewer | undefined = businessClient
    ? async (text: string) => {
        const identity = turnContext.peek();
        if (!identity) return undefined;
        // Phase 6: the review is a child span of the turn. The parent comes
        // from the propagated context, so this works from inside the pi hook
        // without handing the hook a span object.
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
          // Synchronous and non-blocking: the SDK's processor does the export.
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
      // Phase 3 §5: default Pi compaction with a bounded profile. The turn
      // snapshot is re-injected every turn, so compaction can never lose the
      // authoritative business facts.
      compaction: {
        enabled: true,
        reserveTokens: 8_192,
        keepRecentTokens: 16_384,
        ...(options.compaction ?? {}),
      },
    },
  );
  // Phase 8 §8.1: the knowledge tool's transport decides whether an MCP
  // extension is mounted. Default http = Phase 0-7 behaviour, byte for byte.
  const knowledgeTransport = toolMode === "fake" ? "http" : resolveKnowledgeTransport();
  // The prompt must name the tool that is actually declared for this transport.
  const knowledgeToolName = knowledgeTransport === "mcp" ? KNOWLEDGE_MCP_TOOL : KNOWLEDGE_HTTP_TOOL;
  // Whether write tools will actually be on the tool face — not merely whether
  // the mode allows them. `shadow` without a plan store mounts nothing, and a
  // fake-tool agent never mounts business tools at all; the prompt follows the
  // mounted set, because a prompt that advertises tools the model does not have
  // is exactly the drift Phase 10 §② exists to remove.
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

  // Explicit resource loader: our prompt, our extensions, NO ~/.pi or .pi/
  // discovery (plan v2 §8 #4).
  // Phase 8 §8.2: skills are opt-in and additive. Discovery stays OFF in both
  // states — `noSkills: true` keeps the user-level skill directories (pi's own
  // and the Claude-Code-compatible ones) out of a customer-service prompt, and
  // `additionalSkillPaths` is loaded explicitly even then. Measured: flipping
  // discovery on pulled 30 unrelated user skills into the prompt.
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
    // Phase 10 §②: the prompt is composed from the same switch that mounts the
    // write tools, so what the model is told it can do and what its tool face
    // actually offers cannot drift apart.
    systemPrompt: systemPromptFor(knowledgeToolName, { writeTools: writeToolsMounted }),
    // The skills section is composed here rather than by the SDK: the harness
    // owns its prompt in full, so it owns the disclosure that goes into it.
    // Only names and descriptions — the bodies stay on disk until the model
    // asks for one through `skill_load`.
    ...(skillsEnabled
      ? {
          systemPromptOverride: (base: string | undefined) => {
            const loaded = resourceLoader.getSkills().skills;
            if (loaded.length === 0) return base;
            const section = [
              "## 可用技能（Skills）",
              "以下是本服务已注册的知识技能；需要细节时用 skill_load 取正文。技能只说明「怎么说」，任何「能不能」的结论以业务系统结果为准。",
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
    customTools = createBusinessReadTools({
      client,
      turnContext,
      shadowPendingActionFor:
        writeMode === "shadow" && shadowStore
          ? (params) => {
              const identity = turnContext.peek();
              const orderId = String((params as { order_id?: unknown }).order_id ?? "");
              if (!identity || !orderId) return undefined;
              return shadowPendingActionId({
                sessionId: identity.sessionId,
                clientRequestId: identity.clientRequestId,
                orderId,
              });
            }
          : undefined,
    });
    // Phase 8 §8.1: with the MCP transport the knowledge tool is declared by
    // the MCP extension, so the HTTP shell must NOT also be declared — the
    // tool face keeps exactly its Phase 7 size and names change only for this
    // one tool.
    if (knowledgeTransport === "mcp") {
      customTools = customTools.filter((tool) => tool.name !== KNOWLEDGE_HTTP_TOOL);
    }
    // shadow needs a plan store; live needs neither the store nor the
    // synthetic pending id (the runtime owns the real `pending_action`).
    // Same predicate the prompt was composed from, above.
    if (writeToolsMounted) {
      customTools = [
        ...customTools,
        ...createShadowWriteTools({
          mode: writeMode,
          store: shadowStore,
          turnContext,
          client: businessClient,
          receipts: options.receiptStore,
          // Default audit context: derived from the transcript, so production
          // gets a real turn number without any extra plumbing.
          turnIndex:
            options.shadowTurnContext?.turnIndex ?? (() => userTurns(sessionManager) - 1),
          lastUserMessage:
            options.shadowTurnContext?.lastUserMessage ?? (() => lastUserText(sessionManager)),
        }),
      ];
    }
  }
    // Phase 8 §8.2: the on-demand half of skill disclosure. pi exposes loaded
    // skills as user commands only, so an SDK host supplies the model-side
    // entry point itself — narrowly: enum of the registered names, registered
    // only when the switch is on (the tool face grows by exactly this one).
    if (skillsEnabled) {
      const loadedSkills = resourceLoader.getSkills().skills;
      if (loadedSkills.length > 0) {
        customTools = [...customTools, createSkillLoadTool(loadedSkills)];
      }
    }

  // Phase 8 §8.1: the active-tool whitelist IS the tool face — a name that is
  // not listed here is filtered out even if an extension registered it. With
  // the MCP transport the knowledge tool therefore appears under its MCP name
  // instead of the HTTP name: same size, exactly one knowledge tool either way.
  const readToolNames =
    knowledgeTransport === "mcp"
      ? READ_TOOL_NAMES.filter((name) => name !== KNOWLEDGE_HTTP_TOOL)
      : [...READ_TOOL_NAMES];
  if (options.additionalTools?.length) {
    customTools = [...customTools, ...options.additionalTools];
  }

  // `writeToolsMounted`, not `writeToolsEnabled`: the whitelist may only name
  // tools that were actually constructed above. Naming an unmounted write tool
  // (shadow without a plan store) used to put a phantom entry on the tool face.
  const defaultWhitelist: string[] =
    toolMode === "fake"
      ? [...SMARTCS_TOOL_WHITELIST]
      : writeToolsMounted
        ? [...readToolNames, ...SHADOW_WRITE_TOOL_NAMES]
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
    // The built-in MCP extension connects its servers on `session_start`, and
    // that event is only emitted when the embedder binds the extension
    // lifecycle — the CLI modes do it, an SDK host must ask. Bound on this
    // path only, so the default (http) transport keeps the exact Phase 0-7
    // lifecycle, byte for byte.
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
