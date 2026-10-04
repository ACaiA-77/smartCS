/**
 * Boot the REAL Python MCP gateway over its test corpus (Phase 8 §8.1).
 *
 * Same rule as every other fixture here: no mock of the thing under test. The
 * process serves the production token guard and tool definition; only the
 * corpus is the isolated one in `python-impl/tests/mcp_test_corpus.py`.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createConnection } from "node:net";
import { join } from "node:path";
import { PYTHON_IMPL, pythonExecutable } from "./phase1.js";

export const MCP_TEST_TOKEN = "phase8-mcp-test-token-0123456789";
export const MCP_TEST_CORPUS_ANSWER = "退款政策：用户在购买后 7 天内可申请无理由退款";

export interface McpGateway {
  url: string;
  port: number;
  stderr: () => string;
  stop: () => Promise<void>;
}

function probe(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection({ host: "127.0.0.1", port }, () => {
      socket.end();
      resolve(true);
    });
    socket.on("error", () => resolve(false));
    socket.setTimeout(1_000, () => {
      socket.destroy();
      resolve(false);
    });
  });
}

export async function startMcpGateway(options: { port: number; token?: string }): Promise<McpGateway> {
  const child: ChildProcess = spawn(
    pythonExecutable(),
    [join(PYTHON_IMPL, "tests", "mcp_gateway_testserver.py")],
    {
      cwd: PYTHON_IMPL,
      env: {
        ...process.env,
        PYTHONPATH: PYTHON_IMPL,
        EMBEDDING_BACKEND: "hash",
        SMARTCS_MCP_TOKEN: options.token ?? MCP_TEST_TOKEN,
        SMARTCS_MCP_PORT: String(options.port),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderrText = "";
  child.stderr?.on("data", (chunk) => {
    stderrText += String(chunk);
  });

  const deadline = Date.now() + 120_000;
  for (;;) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`mcp gateway exited during startup: ${stderrText}`);
    }
    if (await probe(options.port)) break;
    if (Date.now() > deadline) {
      child.kill();
      throw new Error(`mcp gateway did not start: ${stderrText}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }

  return {
    url: `http://127.0.0.1:${options.port}/mcp`,
    port: options.port,
    stderr: () => stderrText,
    stop: () =>
      new Promise<void>((resolveStop) => {
        if (child.exitCode !== null || child.signalCode !== null) {
          resolveStop();
          return;
        }
        child.once("exit", () => resolveStop());
        child.kill();
        setTimeout(() => {
          if (!child.killed) child.kill("SIGKILL");
          resolveStop();
        }, 5_000).unref?.();
      }),
  };
}
