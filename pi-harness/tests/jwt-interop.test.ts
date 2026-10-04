/**
 * HS256 verification: strictness on its own, and real interop with PyJWT.
 *
 * The harness hand-writes HS256 verification instead of using a JWT library.
 * That is only defensible if the negative space is pinned down and the tokens
 * actually interoperate with the Python side that mints and consumes them —
 * both directions are exercised against the real python-impl code.
 */

import { spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { JwtError, signHs256, verifyHs256 } from "../src/business/jwt-hs256.js";
import { PYTHON_IMPL, pythonExecutable } from "./helpers/phase1.js";
import { pythonEnv } from "../src/config/env.js";
import { mintServiceToken } from "../src/business/python-client.js";

const SECRET = "0123456789abcdef0123456789abcdef0123456789";
const NOW = 1_800_000_000;

function base(overrides: Record<string, unknown> = {}) {
  return { iss: "smartcs", sub: "7", iat: NOW, exp: NOW + 600, jti: "abc", ...overrides };
}

const options = { secret: SECRET, issuer: "smartcs", maxTtlSeconds: 1800, nowSeconds: NOW + 1 };

function runPython(code: string, stdin: string): { ok: boolean; stdout: string; stderr: string } {
  const result = spawnSync(pythonExecutable(), ["-c", code], {
    cwd: PYTHON_IMPL,
    input: stdin,
    encoding: "utf-8",
    env: {
      ...process.env,
      ...pythonEnv(),
      PYTHONPATH: PYTHON_IMPL,
      INTERNAL_SERVICE_JWT_SECRET:
        process.env.INTERNAL_SERVICE_JWT_SECRET ?? "phase1-interop-secret-0123456789abcdef",
    },
  });
  return { ok: result.status === 0, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

describe("verifyHs256 strictness", () => {
  it("accepts a well-formed token and returns its claims", () => {
    const token = signHs256(base(), SECRET);
    expect(verifyHs256(token, options).sub).toBe("7");
  });

  it("rejects a token signed with a different secret", () => {
    const token = signHs256(base(), `${SECRET}x`);
    expect(() => verifyHs256(token, options)).toThrow(JwtError);
  });

  it("rejects a tampered payload (signature covers header.payload)", () => {
    const token = signHs256(base(), SECRET);
    const [h, , s] = token.split(".");
    const forged = Buffer.from(JSON.stringify(base({ sub: "999" }))).toString("base64url");
    expect(() => verifyHs256(`${h}.${forged}.${s}`, options)).toThrow(JwtError);
  });

  it("rejects alg=none and algorithm substitution", () => {
    const header = Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url");
    const payload = Buffer.from(JSON.stringify(base())).toString("base64url");
    expect(() => verifyHs256(`${header}.${payload}.`, options)).toThrow(JwtError);
    const rs = Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })).toString("base64url");
    expect(() => verifyHs256(`${rs}.${payload}.AAAA`, options)).toThrow(JwtError);
  });

  it("rejects expired, over-long, and not-yet-valid tokens", () => {
    expect(() => verifyHs256(signHs256(base({ iat: NOW - 5000, exp: NOW - 1000 }), SECRET), options)).toThrow(JwtError);
    expect(() => verifyHs256(signHs256(base({ exp: NOW + 99999 }), SECRET), options)).toThrow(JwtError);
    expect(() => verifyHs256(signHs256(base({ iat: NOW + 9999, exp: NOW + 9999 + 60 }), SECRET), options)).toThrow(JwtError);
  });

  it("rejects wrong issuer, unexpected audience, and missing claims", () => {
    expect(() => verifyHs256(signHs256(base({ iss: "evil" }), SECRET), options)).toThrow(JwtError);
    // The user token carries no `aud`; one that does must not be accepted.
    expect(() => verifyHs256(signHs256(base({ aud: "smartcs-business-runtime" }), SECRET), options)).toThrow(JwtError);
    const { jti: _omitted, ...noJti } = base();
    expect(() =>
      verifyHs256(signHs256(noJti, SECRET), { ...options, requiredClaims: ["jti"] }),
    ).toThrow(JwtError);
  });

  it("enforces the audience when one is expected", () => {
    const token = signHs256({ ...base(), aud: "smartcs-business-runtime" }, SECRET);
    expect(() =>
      verifyHs256(token, { ...options, audience: "smartcs-pi-harness" }),
    ).toThrow(JwtError);
    expect(verifyHs256(token, { ...options, audience: "smartcs-business-runtime" }).aud).toBe(
      "smartcs-business-runtime",
    );
  });

  it("rejects malformed token shapes", () => {
    for (const bad of ["", "a.b", "a.b.c.d", "!!!.@@@.###", "a.b.c"]) {
      expect(() => verifyHs256(bad, options)).toThrow(JwtError);
    }
  });
});

describe("PyJWT interop", () => {
  const secret = "phase1-interop-secret-0123456789abcdef";

  it("verifies a user token minted by the real python auth/jwt.py", () => {
    const program = `
import os, sys
os.environ["AUTH_JWT_SECRET"] = sys.argv[1]
from auth.jwt import issue_token
sys.stdout.write(issue_token(42))
`;
    const result = spawnSync(pythonExecutable(), ["-c", program, secret], {
      cwd: PYTHON_IMPL,
      encoding: "utf-8",
      env: { ...process.env, PYTHONPATH: PYTHON_IMPL },
    });
    expect(result.status).toBe(0);
    const token = (result.stdout ?? "").trim();
    expect(token.split(".")).toHaveLength(3);

    const claims = verifyHs256(token, {
      secret,
      issuer: "smartcs",
      maxTtlSeconds: 1800,
      requiredClaims: ["sub", "iat", "exp", "iss", "jti"],
    });
    expect(claims.sub).toBe("42");
  });

  it("python internal_api accepts a service token minted by the harness", () => {
    process.env.INTERNAL_SERVICE_JWT_SECRET = secret;
    const token = mintServiceToken({
      accountId: 7,
      sessionId: "interop-session",
      clientRequestId: "interop-request",
    });

    const program = `
import sys
from internal_api.service_jwt import decode_service_token
identity = decode_service_token(sys.stdin.read().strip())
sys.stdout.write(f"{identity.account_id}|{identity.session_id}|{identity.client_request_id}|{identity.business_user_id}")
`;
    const result = runPython(program, token);
    expect(result.stderr).toBe("");
    expect(result.ok).toBe(true);
    expect(result.stdout.trim()).toBe("7|interop-session|interop-request|None");
  });

  it("python internal_api rejects a service token signed with the wrong secret", () => {
    process.env.INTERNAL_SERVICE_JWT_SECRET = secret;
    const token = signHs256(
      {
        iss: "smartcs-pi-harness",
        aud: "smartcs-business-runtime",
        account_id: 7,
        session_id: "s",
        client_request_id: "r",
        iat: Math.floor(Date.now() / 1000),
        exp: Math.floor(Date.now() / 1000) + 60,
      },
      "a-completely-different-secret-value-12345",
    );
    const program = `
import sys, jwt
from internal_api.service_jwt import decode_service_token
try:
    decode_service_token(sys.stdin.read().strip())
    sys.stdout.write("ACCEPTED")
except jwt.InvalidTokenError:
    sys.stdout.write("REJECTED")
`;
    const result = runPython(program, token);
    expect(result.stdout.trim()).toBe("REJECTED");
  });
});
