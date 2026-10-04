import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    // Pi sessions are filesystem-backed; keep test files sequential so the
    // "two processes open the same session id" probe is not perturbed by
    // unrelated concurrent tmp-dir churn.
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
