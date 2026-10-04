/**
 * Public user-token handling at the edge.
 *
 * The harness verifies the signature locally (fast fail, no round trip) and
 * then asks the Business Runtime for the authoritative account state. Identity
 * never comes from the request body, query string or any business field —
 * mirrors auth/dependency.py.
 */

import { COOKIE_NAME, USER_JWT_ISSUER, USER_JWT_MAX_TTL_SECONDS, userJwtSecret } from "../config/env.js";
import { JwtError, verifyHs256 } from "../business/jwt-hs256.js";

export { COOKIE_NAME };

export class AuthError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(detail);
  }
}

export interface UserClaims {
  accountId: number;
  /** Raw token text — forwarded verbatim to the Business Runtime. */
  token: string;
}

function parseCookies(header: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index <= 0) continue;
    out[part.slice(0, index).trim()] = decodeURIComponent(part.slice(index + 1).trim());
  }
  return out;
}

/**
 * Extract and verify the caller's token. `Authorization: Bearer` wins; the
 * HttpOnly cookie is the browser path. If both are present they must agree,
 * so a stale cookie cannot silently override an explicit bearer token.
 */
export function authenticateRequest(headers: {
  authorization?: string;
  cookie?: string;
}): UserClaims {
  const cookieToken = parseCookies(headers.cookie)[COOKIE_NAME];
  let token = cookieToken;

  if (headers.authorization !== undefined) {
    const parts = headers.authorization.split(/\s+/);
    if (parts.length !== 2 || parts[0]?.toLowerCase() !== "bearer" || !parts[1]) {
      throw new AuthError(401, "invalid authentication");
    }
    if (cookieToken && cookieToken !== parts[1]) {
      throw new AuthError(401, "invalid authentication");
    }
    token = parts[1];
  }

  if (!token) throw new AuthError(401, "authentication required");

  let claims: Record<string, unknown>;
  try {
    // The public token carries no `aud` (python auth/jwt.py), so the verifier
    // is told to expect none.
    claims = verifyHs256(token, {
      secret: userJwtSecret(),
      issuer: USER_JWT_ISSUER,
      maxTtlSeconds: USER_JWT_MAX_TTL_SECONDS,
      requiredClaims: ["sub", "iat", "exp", "iss", "jti"],
    });
  } catch (error) {
    if (error instanceof JwtError) throw new AuthError(401, "invalid or expired authentication");
    throw error;
  }
  // `sub` is a decimal account id, canonicalised (no leading zeros / signs).
  const sub = claims.sub;
  if (typeof sub !== "string" || !/^[1-9][0-9]{0,18}$/.test(sub)) {
    throw new AuthError(401, "invalid authentication");
  }
  return { accountId: Number(sub), token };
}
