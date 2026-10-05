"""Phase 10 §⑤: segment timing for the real retrieval chain.

Why this lives in `internal_api/`
---------------------------------
The measurement has to decompose `dense / BM25 / RRF / rerank / serialization`
inside `rag/`, and `rag/` is frozen for this phase. It is therefore done from
the outside, by **wrapping the objects `rag/` hands us at runtime** rather than
by editing it:

  * each `DenseRetriever` / `SparseRetriever` instance's `search` is wrapped;
  * the reranker instance's `rerank` is wrapped;
  * `rag.retriever`'s module-level helpers (`reciprocal_rank_fusion`,
    `global_ranked_candidates`) are wrapped in that module's own namespace.

Nothing in `rag/` changes, no behaviour changes, and every wrapper is
pass-through: it records a duration and returns the original call's value
untouched. If the objects do not look the way this module expects, it raises —
a profiler that silently measures nothing is worse than no profiler.

Usage as a tool:

    python -m internal_api.rag_timing                  # benchmark queries
    python -m internal_api.rag_timing --queries N      # limit
    python -m internal_api.rag_timing --json out.json

Usage in-process (the MCP gateway installs it when SMARTCS_RAG_TIMING=1):

    from internal_api.rag_timing import install_retriever_timing
    install_retriever_timing(retriever)

Read `rag_timing_report()` for the P50/P95 view. This module deliberately
depends on nothing outside the standard library so it can be imported from
anywhere in the tree.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator

#: Segment names, in the order the chain executes them.
SEGMENTS = (
    "dense",
    "bm25",
    "rank_merge",
    "rrf",
    "rerank",
    "retrieve_total",
    "serialize",
)


class SegmentTimer:
    """Bounded, in-process duration recorder (one deque per segment)."""

    def __init__(self, capacity: int = 4000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._dropped = 0

    def record(self, name: str, milliseconds: float) -> None:
        bucket = self._samples[name]
        bucket.append(milliseconds)
        # Oldest-first eviction: a long-running process reports its recent
        # behaviour instead of growing without bound.
        while len(bucket) > self.capacity:
            bucket.pop(0)
            self._dropped += 1

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - started) * 1000.0)

    def samples(self, name: str) -> list[float]:
        return list(self._samples.get(name, ()))

    def names(self) -> list[str]:
        return sorted(self._samples)

    @property
    def dropped(self) -> int:
        return self._dropped

    def reset(self) -> None:
        self._samples.clear()
        self._dropped = 0

    def report(self) -> dict[str, dict[str, float]]:
        """Percentiles per segment. P50/P95 are the ones the review asked for."""
        out: dict[str, dict[str, float]] = {}
        for name in self.names():
            values = sorted(self._samples[name])
            out[name] = {
                "count": float(len(values)),
                "p50": round(_percentile(values, 50.0), 2),
                "p95": round(_percentile(values, 95.0), 2),
                "max": round(values[-1], 2),
                "mean": round(statistics.fmean(values), 2),
                "total": round(sum(values), 2),
            }
        return out


def _percentile(sorted_values: list[float], percent: float) -> float:
    """Nearest-rank percentile — no interpolation, no numpy dependency."""
    if not sorted_values:
        return 0.0
    if percent <= 0:
        return sorted_values[0]
    if percent >= 100:
        return sorted_values[-1]
    rank = int(round((percent / 100.0) * len(sorted_values) + 0.5))
    index = min(max(rank - 1, 0), len(sorted_values) - 1)
    return sorted_values[index]


# --- instance instrumentation ------------------------------------------------


def _wrap(obj: Any, attribute: str, timer: SegmentTimer, segment: str) -> bool:
    original = getattr(obj, attribute, None)
    if not callable(original):
        return False

    def timed(*args: Any, **kwargs: Any) -> Any:
        with timer.measure(segment):
            return original(*args, **kwargs)

    setattr(obj, attribute, timed)
    return True


#: Timers installed in this process, so a service can log a report on demand.
_INSTALLED: list[SegmentTimer] = []


def install_retriever_timing(retriever: Any, timer: SegmentTimer | None = None) -> SegmentTimer:
    """Wrap one `HybridRetriever` instance's collaborators, in place.

    Idempotent per retriever: the wrapped methods are tagged, so installing
    twice cannot double-count.
    """
    timer = timer or SegmentTimer()
    if getattr(retriever, "_smartcs_timed", False):
        return timer

    # Prewarm the lazily-built collaborators so there is an instance to wrap.
    # This is exactly what the first real request would trigger — it does not
    # add work, it only moves the construction before the measurement starts.
    domains = list(retriever._domains(None))  # noqa: SLF001 — documented above
    for domain in domains:
        retriever._artifact_retrievers(domain)  # noqa: SLF001
    if getattr(retriever, "sparse_mode", "") == "global_corpus_v1" and len(domains) > 1:
        retriever._global_sparse_retriever()  # noqa: SLF001

    wrapped: list[str] = []

    def wrap(obj: Any, attribute: str, segment: str, label: str) -> None:
        if _wrap(obj, attribute, timer, segment):
            wrapped.append(label)

    # Wrap BOTH sparse paths. Which one runs depends on the request, not on the
    # configuration: `retriever.retrieve` short-circuits to the global retriever
    # only when `global_corpus_v1` is configured AND more than one domain is
    # selected. A single-domain call therefore uses the per-domain retriever
    # even under `global_corpus_v1` — the first version of this module skipped
    # that case and reported no BM25 samples at all, which is exactly the kind
    # of silent hole a profiler must not have.
    for domain, (dense, sparse) in getattr(retriever, "_domain_retrievers", {}).items():
        wrap(dense, "search", "dense", f"dense:{domain}")
        wrap(sparse, "search", "bm25", f"bm25:{domain}")

    global_sparse = getattr(retriever, "_global_sparse", None)
    if global_sparse is not None:
        wrap(global_sparse, "search", "bm25", "bm25:global")

    wrap(retriever.reranker, "rerank", "rerank", "rerank")

    # Module-level helpers, patched in `rag.retriever`'s own namespace — the
    # retriever calls them as bare names, so that is where the lookup happens.
    import rag.retriever as rag_retriever

    for name, segment in (("reciprocal_rank_fusion", "rrf"), ("global_ranked_candidates", "rank_merge")):
        original = getattr(rag_retriever, name, None)
        if not callable(original):
            raise RuntimeError(f"rag.retriever.{name} is not callable; cannot time the RRF stage")

        def make(fn: Callable[..., Any], seg: str) -> Callable[..., Any]:
            def timed(*args: Any, **kwargs: Any) -> Any:
                with timer.measure(seg):
                    return fn(*args, **kwargs)

            return timed

        setattr(rag_retriever, name, make(original, segment))
        wrapped.append(name)

    wrap(retriever, "retrieve", "retrieve_total", "retrieve")

    if not wrapped:
        raise RuntimeError("no retrieval collaborators were found to time")
    retriever._smartcs_timed = True  # noqa: SLF001
    retriever._smartcs_timer = timer  # noqa: SLF001
    _INSTALLED.append(timer)
    return timer


def timed_serialize(timer: SegmentTimer, fn: Callable[[], Any]) -> Any:
    """Time the caller-side serialization of a tool result (the wire payload)."""
    with timer.measure("serialize"):
        return fn()


# --- CLI profiler ------------------------------------------------------------


def _load_queries(limit: int | None) -> list[tuple[str, str]]:
    """`(query, domain)` pairs from the RAG benchmark, so timing reflects real
    traffic shape rather than invented strings."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "benchmarks" / "rag" / "queries.jsonl"
    if not path.exists():
        raise SystemExit(f"benchmark queries not found: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pairs = [(str(row["query"]), str(row.get("domain") or "")) for row in rows]
    return pairs[:limit] if limit else pairs


def _load_dotenv_best_effort() -> None:
    """Load `python-impl/.env` for the CLI, exactly like `api/main.py` does.

    Guarded so the module keeps its stdlib-only import surface: the MCP gateway
    loads it standalone and must not gain a dependency.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _build_retriever() -> Any:
    """Build the retriever exactly the way the service does."""
    from memory.knowledge import KnowledgeMemory

    import os

    memory = KnowledgeMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
    return memory.get_retriever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Segment latency profile of the real retrieval chain")
    parser.add_argument("--queries", type=int, default=None, help="limit the number of benchmark queries")
    parser.add_argument("--json", type=str, default=None, help="write the full report to this path")
    parser.add_argument("--warmup", type=int, default=1, help="warmup retrievals excluded from the report")
    parser.add_argument(
        "--per-domain",
        action="store_true",
        help=(
            "restrict each query to its benchmark domain. Default is the shape the "
            "knowledge_search tool actually produces: no domain argument, so every "
            "domain is searched and the GLOBAL sparse retriever is used."
        ),
    )
    args = parser.parse_args(argv)

    _load_dotenv_best_effort()
    queries = _load_queries(args.queries)
    print(f"loading production retriever (env: {_env_summary()})", file=sys.stderr)
    cold_start = time.perf_counter()
    retriever = _build_retriever()
    timer = install_retriever_timing(retriever)
    cold_start_ms = (time.perf_counter() - cold_start) * 1000.0
    print(
        f"retriever + model load: {cold_start_ms:.0f} ms "
        f"(reranker={type(retriever.reranker).__name__}, sparse_mode={retriever.sparse_mode})",
        file=sys.stderr,
    )

    def domains_for(domain: str) -> list[str] | None:
        return [domain] if (args.per_domain and domain) else None

    for index in range(max(0, args.warmup)):
        query, domain = queries[index % len(queries)]
        retriever.retrieve(query, domains=domains_for(domain), top_k=3, rerank=True)

    # Everything the report is computed from is measured after warmup, so the
    # numbers describe steady-state per-request cost.
    timer.reset()
    rows: list[dict[str, Any]] = []
    for query, domain in queries:
        started = time.perf_counter()
        hits = retriever.retrieve(query, domains=domains_for(domain), top_k=3, rerank=True)
        payload = timed_serialize(timer, lambda hits=hits: json.dumps(
            [hit.to_dict() if hasattr(hit, "to_dict") else dict(hit) for hit in hits],
            ensure_ascii=False,
        ))
        rows.append(
            {
                "query": query,
                "domain": domain,
                "hits": len(hits),
                "payload_bytes": len(payload.encode("utf-8")),
                "wall_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }
        )

    report = {
        "queries": len(queries),
        "cold_start_ms": round(cold_start_ms, 2),
        "reranker": type(retriever.reranker).__name__,
        "sparse_mode": retriever.sparse_mode,
        "domains": list(retriever._domains(None)),  # noqa: SLF001
        "call_shape": "per-domain" if args.per_domain else "all-domains (production shape)",
        "segments": timer.report(),
        "per_query": rows,
    }
    print(json.dumps({"segments": report["segments"]}, ensure_ascii=False, indent=2))
    print(
        "\nsegment            count     p50      p95      max     mean",
        file=sys.stderr,
    )
    for name, stats in sorted(report["segments"].items(), key=lambda kv: -kv[1]["p50"]):
        print(
            f"{name:<18}{int(stats['count']):>5}{stats['p50']:>9.1f}{stats['p95']:>9.1f}"
            f"{stats['max']:>9.1f}{stats['mean']:>9.1f}",
            file=sys.stderr,
        )
    if args.json:
        from pathlib import Path

        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


def _env_summary() -> str:
    import os

    keys = ("RAG_INDEX_ROOT", "RAG_SPARSE_MODE", "RAG_RERANKER_BACKEND", "EMBEDDING_BACKEND", "EMBEDDING_MODEL")
    return ", ".join(f"{key}={os.getenv(key, '-')}" for key in keys)


def rag_timing_enabled() -> bool:
    """Opt-in switch for the long-running service (`SMARTCS_RAG_TIMING=1`)."""
    import os

    return os.getenv("SMARTCS_RAG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


def active_timers() -> Iterable[SegmentTimer]:
    return tuple(_INSTALLED)


def rag_timing_report() -> dict[str, dict[str, float]]:
    """Merged P50/P95 view over every timer installed in this process."""
    merged = SegmentTimer()
    for timer in _INSTALLED:
        for name in timer.names():
            for value in timer.samples(name):
                merged.record(name, value)
    return merged.report()


if __name__ == "__main__":
    raise SystemExit(main())
