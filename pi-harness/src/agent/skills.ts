/**
 * Progressive skill disclosure — knowledge injection, never authority.
 *
 * Skills live under `skills-copilot/` and are registered through an explicit
 * `additionalSkillPaths` entry. Discovery stays off everywhere else (`noSkills`
 * is flipped rather than relaxed), so `~/.pi` and `.pi/` never become a
 * behaviour source.
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

/** `on` | `off`, default `off`. */
export function resolveSkillsEnabled(raw = process.env[SKILLS_ENV]): boolean {
  const value = (raw ?? "off").trim().toLowerCase();
  if (value === "" || value === "off") return false;
  if (value === "on") return true;
  throw new Error(`invalid ${SKILLS_ENV}: ${raw} (expected on|off)`);
}

/**
 * The server-owned Copilot skills directory (`skills-copilot/`).
 */
export function skillsDir(override = process.env[SKILLS_DIR_ENV]): string {
  if (override && override.trim() !== "") return override;
  return fileURLToPath(new URL("../../skills-copilot/", import.meta.url));
}

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
      "读取某个已注册工作台技能的完整正文（操作规范、整理流程、工具使用建议）。需要该技能的细节时调用；返回的是说明性知识，不代表运行时的任何授权结论。",
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
