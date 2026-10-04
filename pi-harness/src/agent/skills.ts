/**
 * Phase 8 §8.2: progressive skill disclosure — knowledge injection, never authority.
 *
 * Three skills live under this package's own `skills/` directory and are
 * registered through an explicit `additionalSkillPaths` entry. Discovery stays
 * off everywhere else (`noSkills` is flipped rather than relaxed), so `~/.pi`
 * and a project `.pi/` can never become a behaviour source — the same rule the
 * resource loader already follows for prompts and extensions (plan v2 §8 #4).
 *
 * What a skill may do: change *how* something is explained — policy wording,
 * tone, which tool to reach for. What it may never do: take part in any
 * "can this happen" decision. Authorization, the two-phase write flow, ledger
 * idempotency and compliance are decided by the runtime and the tool result;
 * no skill text is consulted there, and the regression cases assert exactly
 * that (the tool face and the authority paths are identical with skills on).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  defineTool,
  type AgentToolResult,
  type Skill,
  type ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export const SKILLS_ENV = "SMARTCS_SKILLS";
export const SKILLS_DIR_ENV = "SMARTCS_SKILLS_DIR";

/** `on` | `off`, default `off`: the default deployment is Phase 7, byte for byte. */
export function resolveSkillsEnabled(raw = process.env[SKILLS_ENV]): boolean {
  const value = (raw ?? "off").trim().toLowerCase();
  if (value === "" || value === "off") return false;
  if (value === "on") return true;
  throw new Error(`invalid ${SKILLS_ENV}: ${raw} (expected on|off)`);
}

/**
 * The server-owned skills directory. Never a user home directory: an explicit
 * override (tests, deployments) or this package's own `skills/`.
 */
export function skillsDir(override = process.env[SKILLS_DIR_ENV]): string {
  return override && override.trim() !== ""
    ? override
    : fileURLToPath(new URL("../../skills/", import.meta.url));
}

/**
 * `skill_load` — the on-demand half of progressive disclosure (Phase 8 §8.2).
 *
 * pi 1.0.1 exposes loaded skills as user-facing commands only; an SDK host with
 * nobody at a keyboard has no way for the model to pull a skill's body. This
 * tool is that missing entry point, and it is deliberately narrow:
 *
 *  * `skill` is a fixed enum of the registered skills — the model cannot ask
 *    for a path, and nothing outside the server-owned directory is reachable;
 *  * it is registered only when the skills switch is on, so the default tool
 *    face is unchanged (P8-7);
 *  * it returns TEXT. No authority path reads it, and the text itself states
 *    that decisions belong to the business system.
 */
export const SKILL_LOAD_TOOL = "skill_load";

/** Strip the YAML frontmatter: the description is already in the prompt. */
function skillBody(filePath: string): string {
  const raw = readFileSync(filePath, "utf-8");
  const frontmatter = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(raw);
  return (frontmatter ? raw.slice(frontmatter[0].length) : raw).trim();
}

export function createSkillLoadTool(skills: readonly Skill[]): ToolDefinition {
  const names = skills.map((skill) => skill.name);
  if (names.length === 0) throw new Error("skill_load needs at least one registered skill");
  const byName = new Map(skills.map((skill) => [skill.name, skill]));
  return defineTool({
    name: SKILL_LOAD_TOOL,
    label: "加载技能正文",
    description:
      "读取某个已注册客服技能的完整正文（政策口径、话术规范、工具使用建议）。需要该技能的细节时调用；返回的是说明性知识，不代表业务系统的任何判定结论。",
    parameters: Type.Object(
      { skill: Type.Union(names.map((name) => Type.Literal(name))) },
      { additionalProperties: false },
    ),
    async execute(_toolCallId, params): Promise<AgentToolResult<unknown>> {
      const requested = String((params as { skill?: unknown }).skill ?? "");
      const found = byName.get(requested);
      if (!found) throw new Error(`未知技能：${requested}`);
      return {
        content: [{ type: "text" as const, text: skillBody(found.filePath) }],
        details: { skill: found.name, source: found.filePath },
      };
    },
  }) as unknown as ToolDefinition;
}
