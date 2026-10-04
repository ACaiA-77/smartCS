/**
 * Fault-injecting HTTP proxy in front of the real Python tool channel.
 *
 * Used to reproduce transport-level scenarios (F2 connection drop, F11
 * injection in the tool result, oversized payloads) without touching any
 * production code and without weakening the "real Python" property of the E2E
 * suite: every request that is not deliberately faulted reaches the real
 * runtime.
 */

import { createServer, request as httpRequest, type Server } from "node:http";

export interface ProxyRule {
  /** Return true to apply this rule to the request. */
  matches?: (context: { method: string; url: string; body: string }) => boolean;
  /** Destroy the socket instead of forwarding (simulates a mid-flight drop). */
  dropConnection?: boolean;
  /** Return this status with a JSON body instead of forwarding. */
  respond?: { status: number; body: unknown };
  /** Forward, then rewrite the JSON response body before returning it. */
  rewriteResponse?: (body: unknown) => unknown;
  /** Hold the request before it reaches the runtime (widens an in-flight window). */
  delayBeforeForwardMs?: number;
  /**
   * Forward, let the runtime do the work, then destroy the caller's socket
   * instead of delivering the answer. This is the F4 shape exactly: the write
   * succeeded and the response was lost on the way back.
   */
  dropResponseAfterForward?: boolean;
}

export interface ToolProxy {
  url: string;
  hits: number;
  stop: () => Promise<void>;
}

export async function startToolProxy(options: {
  port: number;
  target: string;
  rules?: ProxyRule[];
}): Promise<ToolProxy> {
  const rules = options.rules ?? [];
  let hits = 0;

  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk) => chunks.push(chunk as Buffer));
    req.on("end", () => {
      void (async () => {
        hits += 1;
        const body = Buffer.concat(chunks).toString("utf-8");
        const context = { method: req.method ?? "GET", url: req.url ?? "/", body };

        for (const rule of rules) {
          if (rule.matches && !rule.matches(context)) continue;

          if (rule.delayBeforeForwardMs) {
            await new Promise((resolve) => setTimeout(resolve, rule.delayBeforeForwardMs));
          }
          if (rule.dropConnection) {
            // Abort the request mid-flight: the client sees a socket error.
            req.socket.destroy();
            return;
          }
          if (rule.respond) {
            res.writeHead(rule.respond.status, { "Content-Type": "application/json" });
            res.end(JSON.stringify(rule.respond.body));
            return;
          }
          if (rule.dropResponseAfterForward) {
            forward(req, res, options.target, body, undefined, true);
            return;
          }
          if (rule.rewriteResponse) {
            forward(req, res, options.target, body, rule.rewriteResponse);
            return;
          }
        }
        forward(req, res, options.target, body);
      })().catch(() => {
        if (!res.headersSent) {
          res.writeHead(502, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ detail: { code: "proxy_error", message: "proxy rule failed" } }));
        } else {
          res.end();
        }
      });
    });
  });

  await new Promise<void>((resolve) => server.listen(options.port, "127.0.0.1", () => resolve()));

  return {
    url: `http://127.0.0.1:${options.port}`,
    get hits() {
      return hits;
    },
    stop: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  } as ToolProxy;
}

function forward(
  req: Parameters<Server["emit"]>[1] extends never ? never : import("node:http").IncomingMessage,
  res: import("node:http").ServerResponse,
  target: string,
  body: string,
  rewrite?: (value: unknown) => unknown,
  dropAfterUpstream = false,
): void {
  const url = new URL(target);
  const upstream = httpRequest(
    {
      hostname: url.hostname,
      port: url.port,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: url.host, "content-length": Buffer.byteLength(body) },
    },
    (upstreamRes) => {
      const chunks: Buffer[] = [];
      upstreamRes.on("data", (chunk) => chunks.push(chunk as Buffer));
      upstreamRes.on("end", () => {
        if (dropAfterUpstream) {
          // The work is done upstream; the caller just never learns the answer.
          res.socket?.destroy();
          return;
        }
        let payload = Buffer.concat(chunks);
        if (rewrite) {
          try {
            const parsed = JSON.parse(payload.toString("utf-8"));
            payload = Buffer.from(JSON.stringify(rewrite(parsed)), "utf-8");
          } catch {
            /* leave the payload untouched when it is not JSON */
          }
        }
        res.writeHead(upstreamRes.statusCode ?? 502, {
          "Content-Type": upstreamRes.headers["content-type"] ?? "application/json",
          "Content-Length": String(payload.length),
        });
        res.end(payload);
      });
    },
  );
  upstream.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: { code: "proxy_error", message: "upstream failed" } }));
    } else {
      res.end();
    }
  });
  upstream.end(body);
}
