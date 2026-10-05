/**
 * Phase 8 §8.1: `knowledge_search` over MCP (streamable HTTP).
 *
 * `knowledge_search` is the one READ tool that can move behind MCP: it searches
 * a shared corpus and carries no per-user data, so it needs no identity in the
 * channel. The four identity-bound tools stay on the HTTP shells — an MCP
 * connection is long-lived and authenticated per server, not per turn, and
 * smuggling a user identity through it would be a security-model change (see
 * `PHASE8_REPORT.md` and `docs/mcp-feasibility.md`).
 *
 * The HTTP shell is kept as the default and the fallback:
 * `SMARTCS_KNOWLEDGE_TRANSPORT=http` (default) declares it, `mcp` replaces it
 * with the server's `mcp__knowledge__knowledge_search`. Exactly one of the two
 * is declared, so the tool face keeps its size — and every call still runs
 * through pi's tool pipeline, so `tool_call`/`tool_result` hooks (audit,
 * compliance, permissions) apply to the MCP tool the same way.
 */

import { createMcpExtension, type ExtensionFactory } from "@earendil-works/pi-coding-agent";
import { envValue } from "../../config/env.js";

export type KnowledgeTransport = "http" | "mcp";

export const KNOWLEDGE_TRANSPORT_ENV = "SMARTCS_KNOWLEDGE_TRANSPORT";
/** Name the MCP tool gets: `mcp__<server>__<tool>` (SDK naming). */
export const KNOWLEDGE_MCP_TOOL = "mcp__knowledge__knowledge_search";
export const KNOWLEDGE_HTTP_TOOL = "knowledge_search";
export const KNOWLEDGE_MCP_SERVER = "knowledge";

/** Grayscale-style switch: a malformed value is a hard error, never a silent default. */
export function resolveKnowledgeTransport(raw = process.env[KNOWLEDGE_TRANSPORT_ENV]): KnowledgeTransport {
  const value = (raw ?? "http").trim().toLowerCase();
  if (value === "" || value === "http") return "http";
  if (value === "mcp") return "mcp";
  throw new Error(`invalid ${KNOWLEDGE_TRANSPORT_ENV}: ${raw} (expected http|mcp)`);
}

export function knowledgeMcpUrl(): string {
  const url = envValue("SMARTCS_MCP_URL", "http://127.0.0.1:8972/mcp") ?? "";
  if (!/^https?:\/\/\S+$/.test(url)) throw new Error("SMARTCS_MCP_URL must be an http(s) URL");
  return url;
}

/** Server-level credential for the MCP channel. Never a user identity. */
export function knowledgeMcpToken(): string {
  const token = envValue("SMARTCS_MCP_TOKEN") ?? "";
  if (token.length < 16 || token !== token.trim()) {
    throw new Error("SMARTCS_MCP_TOKEN must be at least 16 bytes without surrounding whitespace");
  }
  return token;
}

/**
 * The MCP extension, configured from the environment only: no `mcp.json`, no
 * `~/.pi` discovery — the same explicitness rule the resource loader follows.
 */
export function createKnowledgeMcpExtension(): ExtensionFactory {
  const url = knowledgeMcpUrl();
  const token = knowledgeMcpToken();
  return createMcpExtension({
    loadConfig: () => ({
      servers: [
        {
          name: KNOWLEDGE_MCP_SERVER,
          source: "extension:phase8-knowledge",
          scope: "extension",
          config: {
            type: "http",
            url,
            headers: { Authorization: `Bearer ${token}` },
            // Declared to the model like any other tool: the unified entry's tool
            // face must not depend on the codemode/tool_search machinery.
            exposure: "direct",
            description: "企业知识库检索（共享语料，无用户数据）",
          },
        },
      ],
      errors: [],
    }),
  });
}
