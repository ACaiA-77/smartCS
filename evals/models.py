"""Small serializable result types for deterministic agent evaluations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass
class EvalCheck:
    name: str
    passed: bool
    expected: Any
    actual: Any
    metric: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    passed: bool
    critical: bool
    checks: list[EvalCheck] = field(default_factory=list)
    duration_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["checks"] = [check.to_dict() for check in self.checks]
        return value


@dataclass
class EvalSummary:
    total: int
    passed: int
    failed: int
    pass_rate: float
    routing_accuracy: float
    side_effect_safety_rate: float
    failure_containment_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_cases(cls, cases: Iterable[EvalCaseResult]) -> "EvalSummary":
        cases = list(cases)
        total = len(cases)
        passed = sum(case.passed for case in cases)

        def metric_rate(metric: str) -> float:
            checks = [
                check
                for case in cases
                for check in case.checks
                if check.metric == metric
            ]
            return sum(check.passed for check in checks) / len(checks) if checks else 1.0

        return cls(
            total=total,
            passed=passed,
            failed=total - passed,
            pass_rate=passed / total if total else 1.0,
            routing_accuracy=metric_rate("routing"),
            side_effect_safety_rate=metric_rate("side_effect_safety"),
            failure_containment_rate=metric_rate("failure_containment"),
        )
