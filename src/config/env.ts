/**
 * Phase 0 configuration.
 *
 * Hard constraints (plan v2 §8):
 *  - Runtime cwd and Pi session dir are explicit; never derived from ~/.pi.
 *  - agentDir is explicit; the default ~/.pi/agent must not be a behaviour source.
 *
 * LLM credentials are read from the existing python-impl/.env (read-only
 * reference; this spike never writes to python-impl/).
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const PI_HARNESS_ROOT = resolve(HERE, "..", "..");
export const WORKSPACE_ROOT = resolve(PI_HARNESS_ROOT, "..");

/** Minimal .env parser: KEY=VALUE, `#` comments, optional surrounding quotes. */
export function parseDotEnv(content: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length >= 2) ||
      (value.startsWith("'") && value.endsWith("'") && value.length >= 2)
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

export interface LlmConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  sourceFile: string;
}

/** The model-credential fields a real provider requires, in reporting order. */
const LLM_FIELDS = ["OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_NAME"] as const;

function llmConfigPath(envFile?: string): string {
  return envFile ?? process.env.SMARTCS_PYTHON_ENV_FILE ?? resolve(WORKSPACE_ROOT, "python-impl", ".env");
}

/** Why a real provider cannot be used; `null` when it can. Used for fail-fast. */
export function llmConfigGap(envFile?: string): string | null {
  const file = llmConfigPath(envFile);
  if (!existsSync(file)) return `env file not found: ${file}`;
  const parsed = parseDotEnv(readFileSync(file, "utf-8"));
  const missing = LLM_FIELDS.filter((field) => !parsed[field]);
  return missing.length ? `missing ${missing.join(", ")} in ${file}` : null;
}

/**
 * Read OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME from the existing
 * python-impl/.env. Returns null when the file or any field is missing, which
 * lets callers fall back to the Faux provider instead of fabricating a key.
 */
export function loadLlmConfigFromPythonEnv(envFile?: string): LlmConfig | null {
  const file = llmConfigPath(envFile);
  if (!existsSync(file)) return null;
  const parsed = parseDotEnv(readFileSync(file, "utf-8"));
  const baseUrl = parsed.OPENAI_BASE_URL;
  const apiKey = parsed.OPENAI_API_KEY;
  const model = parsed.MODEL_NAME;
  if (!baseUrl || !apiKey || !model) return null;
  return { baseUrl, apiKey, model, sourceFile: file };
}

export const PROVIDER_MODE_ENV = "SMARTCS_PROVIDER_MODE";

/** Thrown when the process cannot satisfy the provider it was configured for. */
export class ProviderConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProviderConfigError";
  }
}

/**
 * True inside a test runner. Vitest sets VITEST; NODE_ENV=test covers the rest.
 * This is the ONLY thing that licenses the Faux fallback, and it is read from
 * the environment rather than threaded through call sites on purpose: a
 * deployment cannot accidentally claim to be a test, because it does not
 * control these variables the way it controls its own configuration.
 */
export function isTestProcess(): boolean {
  return Boolean(process.env.VITEST) || process.env.NODE_ENV === "test";
}

/**
 * Resolve the provider for this process.
 *
 *   SMARTCS_PROVIDER_MODE=faux    → offline provider. Always allowed; this is
 *                                   how a developer or a test asks for it.
 *   SMARTCS_PROVIDER_MODE=openai  → require a complete LLM config. Refuse to
 *                                   start otherwise.
 *   unset                          → infer: a complete config means openai; no
 *                                   config means faux, but ONLY in a test
 *                                   process. Anywhere else it is a refusal.
 *
 * The third branch is the Phase 9 fix. Until now a served deployment with no
 * model credentials started happily and answered every user from the Faux
 * provider — `/health` was green, the product was broken, and nothing said so.
 * A missing credential is a configuration error and must surface at startup,
 * not as a mysterious degradation. `SMARTCS_PHASE0_PROVIDER=faux` remains
 * supported as the legacy spelling of the explicit offline request.
 */
export function resolveProviderMode(raw = process.env[PROVIDER_MODE_ENV]): "openai" | "faux" {
  const declared = (raw ?? "").trim().toLowerCase();
  if (declared && declared !== "openai" && declared !== "faux") {
    throw new ProviderConfigError(
      `invalid ${PROVIDER_MODE_ENV}: ${raw} (expected openai|faux)`,
    );
  }
  if (process.env.SMARTCS_PHASE0_PROVIDER === "faux" && !declared) return "faux";

  const config = loadLlmConfigFromPythonEnv();
  if (declared === "faux") return "faux";
  if (declared === "openai") {
    if (!config) {
      throw new ProviderConfigError(
        `${PROVIDER_MODE_ENV}=openai but the model configuration is incomplete: ${llmConfigGap()}`,
      );
    }
    return "openai";
  }

  if (config) return "openai";
  if (isTestProcess()) return "faux";
  throw new ProviderConfigError(
    `no model configuration and no explicit provider mode: ${llmConfigGap()}. ` +
      `Set ${PROVIDER_MODE_ENV}=openai after fixing the credentials, or ` +
      `${PROVIDER_MODE_ENV}=faux to run the offline provider deliberately.`,
  );
}

let pythonEnvCache: Record<string, string> | undefined;

/** All key/values from python-impl/.env (cached). Used for shared secrets. */
export function pythonEnv(): Record<string, string> {
  if (pythonEnvCache) return pythonEnvCache;
  const file = process.env.SMARTCS_PYTHON_ENV_FILE ?? resolve(WORKSPACE_ROOT, "python-impl", ".env");
  pythonEnvCache = existsSync(file) ? parseDotEnv(readFileSync(file, "utf-8")) : {};
  return pythonEnvCache;
}

/**
 * Runtime config lookup. `process.env` always wins so tests and deployments can
 * override; python-impl/.env is the shared source of truth for secrets that the
 * Python runtime also needs (single place to rotate them).
 */
export function envValue(name: string, fallback?: string): string | undefined {
  const fromProcess = process.env[name];
  if (fromProcess !== undefined && fromProcess !== "") return fromProcess;
  const fromPython = pythonEnv()[name];
  if (fromPython !== undefined && fromPython !== "") return fromPython;
  return fallback;
}

export function requireEnv(name: string): string {
  const value = envValue(name);
  if (value === undefined) throw new Error(`missing required configuration: ${name}`);
  return value;
}

export interface MysqlConfig {
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
}

export function resolveMysqlConfig(overrides: Partial<MysqlConfig> = {}): MysqlConfig {
  const password = overrides.password ?? envValue("MYSQL_PASSWORD");
  if (!password) throw new Error("MYSQL_PASSWORD is required");
  return {
    host: overrides.host ?? envValue("MYSQL_HOST", "127.0.0.1")!,
    port: overrides.port ?? Number(envValue("MYSQL_PORT", "3307")),
    database: overrides.database ?? envValue("MYSQL_DATABASE", "smartcs_checkpoint")!,
    user: overrides.user ?? envValue("MYSQL_USER", "smartcs")!,
    password,
  };
}

/** Public user-token settings; mirrors python auth/jwt.py. */
export const USER_JWT_ISSUER = "smartcs";
export const USER_JWT_MAX_TTL_SECONDS = 1800;
export const COOKIE_NAME = "smartcs_auth";

/**
 * Internal service-token settings; mirrors python internal_api/service_jwt.py.
 *
 * The audience always names the RECEIVER, so the two directions differ:
 *   harness -> runtime : iss=smartcs-pi-harness        aud=smartcs-business-runtime
 *   runtime -> harness : iss=smartcs-business-runtime  aud=smartcs-pi-harness
 */
export const SERVICE_JWT_ALGORITHM = "HS256";
export const SERVICE_JWT_MAX_TTL_SECONDS = 60;

/** Tokens this harness MINTS (consumed by Python). */
export const SERVICE_TOKEN_ISSUER = "smartcs-pi-harness";
export const SERVICE_TOKEN_AUDIENCE = "smartcs-business-runtime";

/** Tokens this harness ACCEPTS (minted by Python). */
export const RUNTIME_TOKEN_ISSUER = "smartcs-business-runtime";
export const RUNTIME_TOKEN_AUDIENCE = "smartcs-pi-harness";

export function userJwtSecret(): string {
  const secret = envValue("AUTH_JWT_SECRET");
  if (!secret || secret.length < 32 || secret !== secret.trim()) {
    throw new Error("AUTH_JWT_SECRET must contain at least 32 bytes without surrounding whitespace");
  }
  return secret;
}

export function serviceJwtSecret(): string {
  const secret = envValue("INTERNAL_SERVICE_JWT_SECRET");
  if (!secret || secret.length < 32 || secret !== secret.trim()) {
    throw new Error(
      "INTERNAL_SERVICE_JWT_SECRET must contain at least 32 bytes without surrounding whitespace",
    );
  }
  return secret;
}

export function pythonInternalBaseUrl(): string {
  const base = (envValue("PYTHON_INTERNAL_BASE_URL", "http://127.0.0.1:8000") ?? "").replace(/\/+$/, "");
  if (!/^https?:\/\/[^\s]+$/.test(base)) throw new Error("PYTHON_INTERNAL_BASE_URL must be an http(s) origin");
  return base;
}

export interface SmartCsPaths {
  /** Explicit runtime cwd. Never process.cwd() implicitly, never a user home dir. */
  runtimeCwd: string;
  /** Explicit durable session volume directory. */
  sessionDir: string;
  /** Explicit agent dir (auth/models/settings live here, not in ~/.pi/agent). */
  agentDir: string;
}

export function resolveSmartCsPaths(overrides: Partial<SmartCsPaths> = {}): SmartCsPaths {
  return {
    runtimeCwd: overrides.runtimeCwd ?? process.env.SMARTCS_RUNTIME_CWD ?? resolve(PI_HARNESS_ROOT, ".runtime", "cwd"),
    sessionDir: overrides.sessionDir ?? process.env.SMARTCS_PI_SESSION_DIR ?? resolve(PI_HARNESS_ROOT, ".runtime", "pi-sessions"),
    agentDir: overrides.agentDir ?? process.env.SMARTCS_PI_AGENT_DIR ?? resolve(PI_HARNESS_ROOT, ".runtime", "pi-agent"),
  };
}
