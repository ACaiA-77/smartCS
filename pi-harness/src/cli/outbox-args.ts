/**
 * Argument parsing for the memory-outbox CLI.
 *
 * Split out from `outbox.ts` so the argument surface is unit-testable without
 * executing the CLI: importing the entry point would otherwise run `main()`.
 * There is no I/O in here — parsing either returns a decision or throws
 * `UsageError`, and the caller turns that into exit code 2 and the usage text.
 */

export class UsageError extends Error {}

export interface ParsedArgs {
  command: "status" | "retry";
  receiptId?: number;
  failed: boolean;
  limit: number;
}

export function parseArgs(argv: string[]): ParsedArgs {
  const command = argv[0];
  if (command !== "status" && command !== "retry") {
    throw new UsageError(`unknown command: ${command ?? "(none)"}`);
  }
  const parsed: ParsedArgs = { command, failed: false, limit: 10 };
  for (let index = 1; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--receipt-id") {
      const raw = argv[++index];
      const value = Number(raw);
      if (!Number.isInteger(value) || value <= 0) {
        throw new UsageError(`--receipt-id requires a positive integer (got ${raw ?? "(nothing)"})`);
      }
      parsed.receiptId = value;
    } else if (flag === "--failed") {
      parsed.failed = true;
    } else if (flag === "--limit") {
      const raw = argv[++index];
      const value = Number(raw);
      if (!Number.isInteger(value) || value <= 0) {
        throw new UsageError(`--limit requires a positive integer (got ${raw ?? "(nothing)"})`);
      }
      parsed.limit = value;
    } else {
      throw new UsageError(`unknown option: ${flag}`);
    }
  }
  if (command === "retry" && parsed.receiptId === undefined && !parsed.failed) {
    throw new UsageError("retry needs --receipt-id <id> or --failed");
  }
  if (parsed.receiptId !== undefined && parsed.failed) {
    throw new UsageError("--receipt-id and --failed are mutually exclusive");
  }
  return parsed;
}
