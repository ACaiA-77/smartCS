/**
 * Minimal, strict HS256 JWT sign/verify.
 *
 * Deliberately hand-written rather than pulling a general JWT library: the
 * algorithm is fixed (no `alg` negotiation, so no algorithm-confusion class of
 * bug), the accepted claims are enumerated, and the whole surface is ~80 lines
 * that can be read in one sitting. Interop is proven against PyJWT in
 * tests/jwt-interop.test.ts — tokens minted by Python verify here and vice
 * versa.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

const ALGORITHM = "HS256";
const BASE64URL = /^[A-Za-z0-9_-]+$/;

export class JwtError extends Error {}

export interface VerifyOptions {
  secret: string;
  /** Required `iss` value (exact match). */
  issuer: string;
  /**
   * Required `aud` value (exact match). When omitted the token must carry NO
   * audience at all — that keeps a token minted for one purpose from being
   * replayed against a verifier that does not expect an audience.
   */
  audience?: string;
  /** Reject tokens whose lifetime exceeds this. */
  maxTtlSeconds: number;
  /** Reject tokens issued too far in the future (clock skew allowance). */
  clockSkewSeconds?: number;
  /** Claims that must be present. */
  requiredClaims?: string[];
  nowSeconds?: number;
}

function b64urlDecode(segment: string): Buffer {
  if (!BASE64URL.test(segment)) throw new JwtError("malformed token segment");
  const buffer = Buffer.from(segment, "base64url");
  // Reject non-canonical encodings (e.g. trailing bits set) so two different
  // strings can never map to the same signature input.
  if (buffer.toString("base64url") !== segment) throw new JwtError("non-canonical token segment");
  return buffer;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function signHs256(claims: Record<string, unknown>, secret: string): string {
  if (typeof secret !== "string" || secret.length === 0) throw new JwtError("secret required");
  const header = Buffer.from(JSON.stringify({ alg: ALGORITHM, typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const signature = createHmac("sha256", secret).update(`${header}.${payload}`).digest("base64url");
  return `${header}.${payload}.${signature}`;
}

export function verifyHs256(token: string, options: VerifyOptions): Record<string, unknown> {
  if (typeof token !== "string" || token.length === 0 || token.length > 4096) {
    throw new JwtError("invalid token");
  }
  const parts = token.split(".");
  if (parts.length !== 3) throw new JwtError("invalid token");

  const [headerSegment, payloadSegment, signatureSegment] = parts as [string, string, string];

  const header = (() => {
    try {
      return JSON.parse(b64urlDecode(headerSegment).toString("utf-8"));
    } catch {
      throw new JwtError("invalid token header");
    }
  })();
  if (!isPlainObject(header) || header.alg !== ALGORITHM) {
    // No negotiation: anything that is not exactly HS256 is refused, which is
    // what closes the "alg: none" / RS256-confusion class of attack.
    throw new JwtError("unsupported token algorithm");
  }

  const expected = createHmac("sha256", options.secret).update(`${headerSegment}.${payloadSegment}`).digest();
  const actual = b64urlDecode(signatureSegment);
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) {
    throw new JwtError("invalid token signature");
  }

  const claims = (() => {
    try {
      return JSON.parse(b64urlDecode(payloadSegment).toString("utf-8"));
    } catch {
      throw new JwtError("invalid token payload");
    }
  })();
  if (!isPlainObject(claims)) throw new JwtError("invalid token payload");

  const now = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  const skew = options.clockSkewSeconds ?? 30;

  if (claims.iss !== options.issuer) throw new JwtError("invalid token issuer");
  if (options.audience === undefined) {
    if (claims.aud !== undefined) throw new JwtError("unexpected token audience");
  } else if (claims.aud !== options.audience) {
    throw new JwtError("invalid token audience");
  }

  const iat = claims.iat;
  const exp = claims.exp;
  if (typeof iat !== "number" || !Number.isInteger(iat)) throw new JwtError("invalid token iat");
  if (typeof exp !== "number" || !Number.isInteger(exp)) throw new JwtError("invalid token exp");
  if (exp - iat <= 0) throw new JwtError("invalid token lifetime");
  if (exp - iat > options.maxTtlSeconds) throw new JwtError("token lifetime exceeds maximum");
  if (iat > now + skew) throw new JwtError("token issued in the future");
  if (exp < now) throw new JwtError("token expired");

  for (const claim of options.requiredClaims ?? []) {
    if (!(claim in claims)) throw new JwtError(`missing claim: ${claim}`);
  }
  return claims;
}
