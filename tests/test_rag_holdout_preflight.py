from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import scripts.evaluate_rag_retrieval as retrieval_eval
from scripts.evaluate_rag_retrieval import run, validate_benchmark_manifest


INDEX = Path("artifacts/rag_round3/production_indexes")
GOLD = Path("benchmarks/rag_holdout_v1")


def copy_gold(tmp_path: Path) -> Path:
    root = tmp_path / "gold"
    shutil.copytree(GOLD, root)
    return root


def test_holdout_preflight_checks_file_hashes_before_models(tmp_path):
    root = copy_gold(tmp_path)
    validate_benchmark_manifest(root, INDEX)
    (root / "qrels.jsonl").write_bytes((root / "qrels.jsonl").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="file hash mismatch for qrels.jsonl"):
        run(benchmark_root=root, artifact_root=INDEX, output_root=tmp_path / "out")


def test_holdout_preflight_requires_pinned_explicit_group_file(tmp_path):
    root = copy_gold(tmp_path)
    with pytest.raises(ValueError, match="requires --qrel-groups"):
        run(benchmark_root=root, artifact_root=INDEX, output_root=tmp_path / "out")
    other_groups = tmp_path / "other_groups.jsonl"
    other_groups.write_bytes((root / "qrel_groups.jsonl").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="group hash mismatch"):
        run(benchmark_root=root, artifact_root=INDEX, output_root=tmp_path / "out", qrel_groups_path=other_groups)


def test_valid_holdout_reaches_model_gate_after_preflight(tmp_path, monkeypatch):
    root = copy_gold(tmp_path)
    monkeypatch.setattr(retrieval_eval, "validate", lambda **kwargs: {"status": "missing_models"})
    result = run(
        benchmark_root=root, artifact_root=INDEX, output_root=tmp_path / "out",
        qrel_groups_path=root / "qrel_groups.jsonl",
    )
    assert result["status"] == "BLOCKED"
    manifest_path = root / "benchmark_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["qrel_groups_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="requires qrel_groups_sha256"):
        run(benchmark_root=root, artifact_root=INDEX, output_root=tmp_path / "out",
            qrel_groups_path=root / "qrel_groups.jsonl")


def test_legacy_manifest_has_no_new_hash_requirement():
    root = Path("benchmarks/rag")
    manifest = json.loads((root / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert "file_sha256" not in manifest
    validate_benchmark_manifest(root, INDEX)
