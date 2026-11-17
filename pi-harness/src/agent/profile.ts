/**
 * Deployment profile switch (`SMARTCS_PROFILE`).
 *
 * The default deployment is the e-commerce customer-service agent, byte for
 * byte. `copilot` selects the personal-workbench persona, prompt, skills and
 * tool surface. The switch lives here — not in `env.ts` — because it selects an
 * agent SHAPE (prompt + tool face), not a transport or credential.
 */

export type AgentProfile = "ecommerce" | "copilot";

export const PROFILE_ENV = "SMARTCS_PROFILE";

/**
 * Resolve the profile from the environment.
 *
 * Empty means "ecommerce": an unset variable must never silently turn a
 * customer-service deployment into a personal agent. An unrecognised value is
 * a configuration error, not a fallback — a typo must not choose a persona.
 */
export function resolveProfile(raw = process.env[PROFILE_ENV]): AgentProfile {
  const value = (raw ?? "ecommerce").trim().toLowerCase();
  if (value === "" || value === "ecommerce") return "ecommerce";
  if (value === "copilot") return "copilot";
  throw new Error(`invalid ${PROFILE_ENV}: ${raw} (expected ecommerce|copilot)`);
}
