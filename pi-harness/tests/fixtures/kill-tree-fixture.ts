/**
 * A THREE-level process tree, deliberately: `tsx` CLI -> this fixture ->
 * a grandchild server. It mirrors how every spawned harness in the suite is
 * actually laid out, where the process holding the listening port is not the
 * one `spawn()` returned.
 *
 * Used by `tests/harness-kill-tree.test.ts` to prove `hardKill` kills the tree
 * and not only the direct child.
 */

import { spawn } from "node:child_process";

const port = process.argv[2];
if (!port) throw new Error("usage: kill-tree-fixture.ts <port>");

const grandchild = spawn(
  process.execPath,
  [
    "-e",
    `require("node:http").createServer((req, res) => res.end("ok")).listen(${Number(port)}, "127.0.0.1");`,
  ],
  { stdio: "ignore" },
);

// Stay alive the way the harness does; the test's kill is the only way out.
setInterval(() => {
  if (grandchild.exitCode !== null) process.exit(grandchild.exitCode ?? 0);
}, 250);
