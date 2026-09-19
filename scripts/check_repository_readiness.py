"""Deterministic local checks for repository commit readiness."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from fnmatch import fnmatch
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    ".env.example",
    ".env.docker.example",
    "evals/runner.py",
    "evals/scenarios.py",
    "tracing/observability.py",
    "tests/test_agent_eval.py",
    "tests/test_eval_failure_injection.py",
    "tests/test_runtime_observability.py",
)
PRODUCTION_DIRS = ("agents", "api", "mcp", "memory", "refunds", "tickets", "sandbox", "tracing")
SOURCE_RULES = (
    (("agents",), (".call_tool(",)),
    (
        PRODUCTION_DIRS,
        ("import evals", "from evals", "FaultInject", "INJECT_FAILURE", "FAULT_MODE"),
    ),
    (
        ("agents", "api", "requirements.txt", "requirements-dev.txt"),
        ("langgraph", "StateGraph", "MemorySaver"),
    ),
    (("agents", "api", "memory"), ("WorkingMemory", "_wm_context")),
)


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def find_forbidden_tracked_files(paths: Iterable[str]) -> list[str]:
    """Return tracked paths that must remain local-only artifacts or secrets."""

    violations = []
    for raw_path in paths:
        path = _normalize_path(raw_path)
        parts = path.split("/")
        if (
            path in {".env", ".env.docker", "nul"}
            or path.startswith("artifacts/")
            or fnmatch(path, "data/*.db")
            or path.startswith("vector_store/")
            or "__pycache__" in parts
            or "pycache" in parts
            or path.endswith(".pyc")
        ):
            violations.append(path)
    return violations


def _matches_scope(path: str, scope: str) -> bool:
    return path == scope or path.startswith(f"{scope}/")


def find_source_violations(source_texts: Mapping[str, str]) -> list[str]:
    """Find prohibited patterns in injected production source text."""

    violations = []
    for raw_path, text in source_texts.items():
        path = _normalize_path(raw_path)
        for scopes, patterns in SOURCE_RULES:
            if not any(_matches_scope(path, scope) for scope in scopes):
                continue
            for pattern in patterns:
                if pattern in text:
                    violations.append(f"{path}: prohibited pattern {pattern!r}")
    return violations


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def _source_texts(root: Path) -> dict[str, str]:
    paths = []
    for directory in PRODUCTION_DIRS:
        base = root / directory
        if base.exists():
            paths.extend(path for path in base.rglob("*.py") if path.is_file())
    for filename in ("requirements.txt", "requirements-dev.txt"):
        path = root / filename
        if path.is_file():
            paths.append(path)
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(paths)
    }


def check_repository(
    root: Path = ROOT,
    *,
    tracked_files: Iterable[str] | None = None,
    source_texts: Mapping[str, str] | None = None,
) -> list[str]:
    failures = [
        f"missing required file: {relative}"
        for relative in REQUIRED_FILES
        if not (root / relative).is_file()
    ]
    files = _tracked_files(root) if tracked_files is None else list(tracked_files)
    failures.extend(f"forbidden tracked file: {path}" for path in find_forbidden_tracked_files(files))
    texts = _source_texts(root) if source_texts is None else source_texts
    failures.extend(find_source_violations(texts))
    return failures


def main() -> int:
    try:
        failures = check_repository()
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"Repository readiness: FAIL\nchecker error: {exc}")
        return 1
    if failures:
        print("Repository readiness: FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print("Repository readiness: PASS")
    print(f"checks={len(REQUIRED_FILES) + 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
