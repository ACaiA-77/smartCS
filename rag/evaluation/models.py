"""Benchmark record contracts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BenchmarkQuery:
    query_id: str
    query: str
    domain: str
    kind: str
    qrels: dict[str, int] = field(default_factory=dict)


__all__ = ["BenchmarkQuery"]
