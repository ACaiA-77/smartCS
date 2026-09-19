from __future__ import annotations

from pathlib import Path
import re
import subprocess

from scripts.check_repository_readiness import (
    check_repository,
    find_forbidden_tracked_files,
    find_source_violations,
)


ROOT = Path(__file__).resolve().parents[1]


def test_checker_passes_current_repository():
    assert check_repository(ROOT) == []


def test_checker_detects_forbidden_production_pattern():
    violations = find_source_violations({"agents/synthetic.py": "server.call_tool({})"})
    assert any(".call_tool(" in violation for violation in violations)


def test_checker_detects_forbidden_tracked_artifact_without_index_mutation():
    violations = find_forbidden_tracked_files(
        ["safe.py", "build/__pycache__/module.pyc", "artifacts/run.txt"]
    )
    assert "build/__pycache__/module.pyc" in violations
    assert "artifacts/run.txt" in violations


def test_docker_example_has_container_paths_and_placeholder_secret():
    text = (ROOT / ".env.docker.example").read_text(encoding="utf-8")
    assert "REDIS_URL=redis://smartcs-redis:6379/0" in text
    assert "ORDER_DB_PATH=/app/data/orders.db" in text
    assert "FAISS_INDEX_PATH=/app/vector_store/faiss_index" in text
    assert "OTEL_SDK_DISABLED=true" in text
    credential_assignments = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if re.search(r"(?:KEY|TOKEN|PASSWORD|SECRET)$", name, re.IGNORECASE):
            credential_assignments.append((name, value))
            assert not value or re.fullmatch(r"your-[a-z0-9-]+-here", value, re.IGNORECASE)
    assert any(name == "OPENAI_API_KEY" for name, _ in credential_assignments)


def test_env_example_disables_local_otel_exporter():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "OTEL_SDK_DISABLED=true" in text


def test_gitignore_keeps_artifacts_local_but_not_evals():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/artifacts/" in lines
    assert "/nul" in lines
    for path, ignored in (
        ("evals/runner.py", False),
        ("artifacts/synthetic.txt", True),
        ("nul", True),
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", path],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == (0 if ignored else 1)


def test_dockerignore_excludes_review_and_eval_inputs():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in ("artifacts/", "evals/", "tests/", "nul"):
        assert entry in lines


def test_workflow_has_ordered_quality_gates_and_offline_environment():
    text = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
    quality = text.split("  build-and-publish:", 1)[0]
    gates = (
        "python -m scripts.check_repository_readiness",
        "python -m pytest -q",
        "python -m evals.runner --json",
    )
    positions = [quality.index(gate) for gate in gates]
    assert positions == sorted(positions)
    assert 'OTEL_SDK_DISABLED: "true"' in quality
    assert 'EMBEDDING_BACKEND: "hash"' in quality


def test_workflow_does_not_grant_package_write_globally():
    text = (ROOT / ".github/workflows/build-image.yml").read_text(encoding="utf-8")
    quality = text.split("  build-and-publish:", 1)[0]
    global_permissions = text.split("jobs:", 1)[0]
    publish_job = text.split("  build-and-publish:", 1)[1]
    assert "packages: write" not in global_permissions
    assert "packages: write" not in quality
    assert "packages: write" in publish_job
