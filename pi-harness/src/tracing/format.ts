/**
 * Human-readable rendering of exported spans (Phase 6 diagnostics).
 *
 * Used by `scripts/phase6-span-tree.ts` and by the acceptance tests to quote
 * what the exporter actually held, rather than what the assertions expected to
 * find. Deliberately dependency-free: a span id is shown in full (it is the
 * link to the Python side) and attributes are printed in insertion order.
 */

import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

function attributeText(span: ReadableSpan, skip: ReadonlySet<string>): string {
  return Object.entries(span.attributes)
    .filter(([key]) => !skip.has(key))
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" ");
}

/**
 * Render spans of one trace as an indented tree, parents before children.
 * Spans whose parent is not in the set are treated as roots, so a partial
 * export still renders (with the missing link called out).
 */
export function formatSpanTree(
  spans: ReadableSpan[],
  options: { traceId?: string; skipAttributes?: ReadonlySet<string> } = {},
): string {
  const selected = options.traceId
    ? spans.filter((span) => span.spanContext().traceId === options.traceId)
    : [...spans];
  const skip = options.skipAttributes ?? new Set<string>();
  const byParent = new Map<string, ReadableSpan[]>();
  const rootKey = "";
  for (const span of selected) {
    const parent = span.parentSpanContext?.spanId ?? rootKey;
    const bucket = byParent.get(parent);
    if (bucket) bucket.push(span);
    else byParent.set(parent, [span]);
  }

  const lines: string[] = [];
  const visited = new Set<string>();
  const render = (parentId: string, depth: number): void => {
    for (const span of byParent.get(parentId) ?? []) {
      const context = span.spanContext();
      if (visited.has(context.spanId)) continue;
      visited.add(context.spanId);
      const indent = "  ".repeat(depth);
      const parentNote = span.parentSpanContext?.spanId
        ? ` parent=${span.parentSpanContext.spanId}`
        : " (root)";
      lines.push(
        `${indent}${span.name} span=${context.spanId}${parentNote} ${attributeText(span, skip)}`.trimEnd(),
      );
      render(context.spanId, depth + 1);
    }
  };
  render(rootKey, 0);

  // Anything whose parent was not exported is still worth showing.
  for (const span of selected) {
    const context = span.spanContext();
    if (visited.has(context.spanId)) continue;
    lines.push(
      `${span.name} span=${context.spanId} parent=${span.parentSpanContext?.spanId ?? "missing"} ${attributeText(span, skip)}`.trimEnd(),
    );
  }
  return lines.join("\n");
}
