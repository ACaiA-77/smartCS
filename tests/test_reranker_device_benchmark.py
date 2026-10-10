"""Offline contract tests: no real torch/model/CUDA loader is needed."""
from __future__ import annotations

import copy
import json
import math
import struct
import sys
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import benchmark_reranker_devices as runner


@pytest.fixture
def inputs(tmp_path):
    snapshot = tmp_path / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"model_type":"xlm-roberta"}', encoding="utf-8")
    # Tiny, structurally valid tensor container for validator tests only.
    header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (snapshot / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)
    return {
        "schema_version": 1, "model_id": runner.MODEL_ID, "model_path": str(snapshot),
        "max_chars": 768, "production_pairs": 9, "quality_pairs": 20,
        "metadata": {"generated_at": "2026-10-10T00:00:00Z", "benchmark_version": "fixture-v1",
                     "benchmark_root": "offline-benchmark", "artifact_root": "offline-index",
                     "source_hashes": {"fixture.json": "a" * 64}},
        "batches": [
            {"query_id": f"q{i}", "domain": ("finance", "commerce")[i % 2], "query": f"query {i}",
             "candidates": [{"chunk_id": f"c{n:02}", "document": str(n), "original_chars": len(str(n)),
                             "truncated_chars": len(str(n)), "extra": {"untouched": True}}
                            for n in range(20)],
             # Deliberately include a relevant chunk not in the candidate pool.
             "qrels": {"c19": 2, "c18": 1, "missing-from-candidates": 1}}
            for i in range(60)
        ],
    }


def options(**overrides):
    result = dict(device="cpu", dtype="fp32", timing_queries=12, timing_repeats=2,
                  quality_queries=60, threads=None, warmups=3, skip_quality=False)
    result.update(overrides)
    return Namespace(**result)


class FakePredictor:
    def __init__(self):
        self.calls = []
        self.internal_batches = []

    def predict(self, pairs, *, batch_size, show_progress_bar, convert_to_numpy):
        assert batch_size == 9
        assert show_progress_bar is False
        assert convert_to_numpy is True
        self.calls.append(list(pairs))
        self.internal_batches.append([len(pairs[i:i + batch_size]) for i in range(0, len(pairs), batch_size)])
        return [float(document) for _, document in pairs]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.01
        return self.now


def test_valid_inputs_and_sha256(inputs, tmp_path):
    import hashlib

    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    loaded, digest, snapshot = runner.load_inputs(path)
    assert loaded == inputs
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot["revision"] == "a" * 40
    assert snapshot["weights"][0]["bytes"] > 0


@pytest.mark.parametrize("path", [r"\\server\share\snapshot", "//server/share/snapshot",
                                  r"\\?\UNC\server\share\snapshot"])
def test_snapshot_rejects_unc_before_filesystem_access(monkeypatch, path):
    is_dir = Mock(side_effect=AssertionError("remote filesystem must not be accessed"))
    monkeypatch.setattr(runner.Path, "is_dir", is_dir)
    with pytest.raises(ValueError, match="UNC/network"):
        runner.validate_snapshot(path)
    is_dir.assert_not_called()


def test_inputs_reject_unc_before_read(monkeypatch):
    read = Mock(side_effect=AssertionError("remote input must not be read"))
    monkeypatch.setattr(runner.Path, "read_bytes", read)
    with pytest.raises(ValueError, match="UNC/network"):
        runner.load_inputs(runner.Path(r"\\server\share\inputs.json"))
    read.assert_not_called()


@pytest.mark.parametrize("argument", ["--inputs", "--output"])
def test_cli_rejects_unc_before_resolving_paths(monkeypatch, argument):
    resolve = Mock(side_effect=AssertionError("remote path must not be resolved"))
    monkeypatch.setattr(runner.Path, "resolve", resolve)
    options = {"--inputs": "inputs.json", "--output": "output.json"}
    options[argument] = r"\\server\share\file.json"
    with pytest.raises(ValueError, match="UNC/network"):
        runner.main(["--inputs", options["--inputs"], "--output", options["--output"],
                     "--device", "cpu", "--dtype", "fp32"])
    resolve.assert_not_called()


@pytest.mark.parametrize("field", ["schema_version", "model_id", "model_path", "max_chars", "production_pairs",
                                   "quality_pairs", "metadata", "batches"])
def test_missing_top_fields_rejected(inputs, field):
    del inputs[field]
    with pytest.raises(ValueError, match="missing fields"):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("field", ["query_id", "domain", "query", "candidates", "qrels"])
def test_missing_batch_fields_rejected(inputs, field):
    del inputs["batches"][0][field]
    with pytest.raises(ValueError, match="missing fields"):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("field", ["chunk_id", "document", "original_chars", "truncated_chars"])
def test_missing_candidate_fields_rejected(inputs, field):
    del inputs["batches"][0]["candidates"][0][field]
    with pytest.raises(ValueError, match="missing fields"):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"model_id": "online-model"},
    {"max_chars": 769}, {"production_pairs": 8}, {"quality_pairs": 19},
    {"metadata": {}}, {"batches": []}, {"model_path": "relative/snapshot"},
])
def test_invalid_top_values(inputs, changes):
    inputs.update(changes)
    with pytest.raises(ValueError):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("field", ["generated_at", "benchmark_version", "benchmark_root", "artifact_root", "source_hashes"])
def test_missing_provenance_rejected(inputs, field):
    del inputs["metadata"][field]
    with pytest.raises(ValueError, match="missing fields"):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("hashes", [{}, {"fixture": "not-a-hash"}, {"fixture": "x" * 64}])
def test_invalid_provenance_hashes(inputs, hashes):
    inputs["metadata"]["source_hashes"] = hashes
    with pytest.raises(ValueError):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("case",  ["few_candidates", "empty_candidates", "duplicate_chunk", "duplicate_query",
                                 "blank_query", "empty_qrels", "negative_grade", "bool_grade", "group_qrels",
                                 "no_positive_grade", "char_mismatch", "over_budget", "short_original",
                                 "nonalternating", "third_domain"])
def test_invalid_batch_values(inputs, case):
    batch = inputs["batches"][0]
    if case == "few_candidates":
        batch["candidates"] = batch["candidates"][:9]
    elif case == "empty_candidates":
        batch["candidates"] = []
    elif case == "duplicate_chunk":
        batch["candidates"][1]["chunk_id"] = batch["candidates"][0]["chunk_id"]
    elif case == "duplicate_query":
        inputs["batches"][1]["query_id"] = batch["query_id"]
    elif case == "blank_query":
        batch["query"] = " "
    elif case == "empty_qrels":
        batch["qrels"] = {}
    elif case == "negative_grade":
        batch["qrels"]["c19"] = -1
    elif case == "bool_grade":
        batch["qrels"]["c19"] = True
    elif case == "group_qrels":
        batch["qrels"]["c19"] = {"group_id": "fact", "relevance": 2}
    elif case == "no_positive_grade":
        batch["qrels"] = {"c19": 0}
    elif case == "char_mismatch":
        batch["candidates"][0]["truncated_chars"] = 3
    elif case == "over_budget":
        batch["candidates"][0].update(document="x" * 769, original_chars=769, truncated_chars=769)
    elif case == "short_original":
        batch["candidates"][0]["original_chars"] = 0
    elif case == "nonalternating":
        inputs["batches"][1]["domain"] = batch["domain"]
    elif case == "third_domain":
        inputs["batches"][1]["domain"] = "third"
    with pytest.raises(ValueError):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("case", ["no_config", "no_weights", "empty_weights", "placeholder_weights", "truncated_weights",
                                 "missing_shard"])
def test_snapshot_refuses_absent_or_placeholder_weights(inputs, case):
    from pathlib import Path

    path = Path(inputs["model_path"])
    weight = path / "model.safetensors"
    if case == "no_config":
        (path / "config.json").unlink()
    elif case == "no_weights":
        weight.unlink()
    elif case == "empty_weights":
        weight.write_bytes(b"")
    elif case == "placeholder_weights":
        weight.write_bytes(b"this is not real local weights")
    elif case == "truncated_weights":
        weight.write_bytes(weight.read_bytes()[:-1])
    elif case == "missing_shard":
        weight.unlink()
        (path / "model.safetensors.index.json").write_text(
            '{"weight_map":{"weight":"model-00001-of-00001.safetensors"}}', encoding="utf-8")
    with pytest.raises(ValueError):
        runner.validate_inputs(inputs)


@pytest.mark.parametrize("values,percent,expected", [([1], 95, 1), (list(range(1, 21)), 50, 10),
                                                     (list(range(1, 21)), 95, 19),
                                                     (list(range(1, 25)), 95, 23),
                                                     ([9, 1, 4], 50, 4)])
def test_percentiles_are_nearest_rank(values, percent, expected):
    assert runner.nearest_rank(values, percent) == expected


@pytest.mark.parametrize("values,percent", [([], 50), ([1], 0), ([1], 101)])
def test_invalid_percentile(values, percent):
    with pytest.raises(ValueError):
        runner.nearest_rank(values, percent)


def test_latency_mean_and_max():
    summary = runner.latency_summary([1, 2, 3, 100])
    assert summary == {"count": 4, "p50": 2, "p95": 100, "mean": 26.5, "max": 100,
                       "total": 106, "percentile_method": "nearest-rank"}


def test_sort_ties_by_chunk_id_without_mutating():
    candidates = [{"chunk_id": "z"}, {"chunk_id": "a"}, {"chunk_id": "m"}]
    before = copy.deepcopy(candidates)
    assert runner.rank_scores(candidates, [1, 1, 2]) == ["m", "a", "z"]
    assert candidates == before
    assert runner.rank_scores(list(reversed(candidates)), [2, 1, 1]) == ["m", "a", "z"]


@pytest.mark.parametrize("scores", [[1], [math.nan, 1], [math.inf, 1], [-math.inf, 1]])
def test_bad_prediction_scores(scores):
    with pytest.raises(ValueError):
        runner.rank_scores([{"chunk_id": "a"}, {"chunk_id": "b"}], scores)


def test_timing_quality_batching_metrics_and_input_immutability(inputs):
    from rag.evaluation.metrics import evaluate_ranking

    before = copy.deepcopy(inputs)
    predictor = FakePredictor()
    report = runner.run_predictions(predictor, inputs, options(), clock=FakeClock())
    assert inputs == before
    assert len(report["timing"]["rows"]) == 24
    assert report["timing"]["latency_ms"]["count"] == 24
    assert report["warmups"]["count"] == 3
    assert report["warmups"]["included_in_timing"] is False
    assert len(predictor.calls) == 1 + 3 + 24 + 60
    assert all(len(pairs) == 9 for pairs in predictor.calls[:28])
    assert all(shape == [9] for shape in predictor.internal_batches[:28])
    assert all(shape == [9, 9, 2] for shape in predictor.internal_batches[28:])
    assert [r["repeat"] for r in report["timing"]["rows"]] == [1] * 12 + [2] * 12
    assert report["timing"]["by_domain"]["finance"]["count"] == 12
    quality = report["quality"]
    assert quality["count"] == 60
    row = quality["rows"][0]
    assert row["qrels"] == inputs["batches"][0]["qrels"]
    assert len(row["scored"]) == len(row["order"]) == 20
    assert row["candidate_ids"] == [f"c{i:02}" for i in range(20)]
    assert row["order"] == [f"c{i:02}" for i in reversed(range(20))]
    assert row["metrics"] == evaluate_ranking(row["order"], row["qrels"], cutoffs=(10,))
    assert row["metrics"]["recall@10"] == 2 / 3  # NOT 1.0: absent relevant chunk counts.
    assert row["metrics"]["mrr@10"] == 1
    assert row["metrics"]["ndcg@10"] < 1
    assert quality["overall"]["recall@10"] == pytest.approx(2 / 3)
    assert report["acceptance_gate"]["status"] == "not_evaluated"


def test_subset_and_skip_quality(inputs):
    predictor = FakePredictor()
    report = runner.run_predictions(predictor, inputs, options(quality_queries=12, warmups=0), clock=FakeClock())
    assert report["quality"]["count"] == 12
    assert report["quality"]["rows"][-1]["query_id"] == "q11"
    predictor = FakePredictor()
    skipped = runner.run_predictions(predictor, inputs, options(skip_quality=True, warmups=0), clock=FakeClock())
    assert skipped["quality"] == {"skipped": True, "scoring_mode": "ordinary_chunk_id",
                                  "count": 0, "overall": {}, "by_domain": {}, "rows": []}
    assert all(len(pairs) == 9 for pairs in predictor.calls)


def test_cuda_timing_synchronizes_on_both_sides(inputs):
    events = []
    predictor = FakePredictor()
    original = predictor.predict

    def predict(*args, **kwargs):
        events.append("predict")
        return original(*args, **kwargs)

    predictor.predict = predict

    def clock():
        events.append("clock")
        return len(events) * 0.01

    runner.predict_batch(predictor, inputs["batches"][0], 9,
                         sync=lambda: events.append("synchronize"), clock=clock)
    assert events == ["synchronize", "clock", "predict", "synchronize", "clock"]


def test_prediction_failure_is_not_retried_or_cpu_fallback(inputs):
    predictor = SimpleNamespace(predict=Mock(side_effect=RuntimeError("CUDA out of memory")))
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        runner.predict_batch(predictor, inputs["batches"][0], 9)
    assert predictor.predict.call_count == 1


@pytest.mark.parametrize("overrides", [{"device": "cpu", "dtype": "fp16"}, {"timing_queries": 0},
                                        {"timing_queries": 61}, {"quality_queries": 61}, {"threads": 0},
                                        {"timing_repeats": 0}, {"warmups": -1}])
def test_invalid_options(overrides):
    with pytest.raises(ValueError):
        runner.validate_options(options(**overrides))


def fake_torch(available):
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: available, set_device=Mock(), synchronize=Mock(),
                             reset_peak_memory_stats=Mock()),
        backends=SimpleNamespace(cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=True)),
                                 cudnn=SimpleNamespace(allow_tf32=True)),
        set_num_threads=Mock(), set_float32_matmul_precision=Mock(),
        float16="torch.float16", float32="torch.float32",
    )


def test_cuda_unavailable_refused():
    torch = fake_torch(False)
    with pytest.raises(RuntimeError, match="fallback is forbidden"):
        runner.configure_torch(torch, options(device="cuda"))
    torch.cuda.set_device.assert_not_called()


def test_cuda_tf32_disabled_and_peak_reset():
    torch = fake_torch(True)
    runner.configure_torch(torch, options(device="cuda", threads=4))
    assert torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.backends.cudnn.allow_tf32 is False
    torch.set_float32_matmul_precision.assert_called_once_with("highest")
    torch.set_num_threads.assert_called_once_with(4)
    torch.cuda.set_device.assert_called_once_with(0)
    torch.cuda.synchronize.assert_called_once_with(0)
    torch.cuda.reset_peak_memory_stats.assert_called_once_with(0)


def test_cpu_does_not_synchronize_or_reset_cuda():
    torch = fake_torch(False)
    runner.configure_torch(torch, options())
    torch.cuda.synchronize.assert_not_called()
    torch.cuda.reset_peak_memory_stats.assert_not_called()


def parameter(dtype="torch.float16", device="cuda:0"):
    return SimpleNamespace(dtype=dtype, device=device, is_floating_point=lambda: True, numel=lambda: 10)


def parameter_predictor(parameters):
    return SimpleNamespace(model=SimpleNamespace(parameters=lambda: iter(parameters)))


def test_actual_model_dtype_and_device_verified():
    result = runner.inspect_parameters(parameter_predictor([parameter()]), "cuda", "fp16")
    assert result["verified_dtype"] == "fp16"
    assert result["floating_parameter_dtypes"] == {"torch.float16": 1}
    assert result["parameter_count"] == 10
    cpu = runner.inspect_parameters(parameter_predictor([parameter("torch.float32", "cpu")]), "cpu", "fp32")
    assert cpu["parameter_devices"] == {"cpu": 1}


@pytest.mark.parametrize("parameters", [[], [parameter("torch.float32")], [parameter(device="cpu")],
                                       [parameter(), parameter("torch.float32")]])
def test_mixed_wrong_dtype_or_device_refused(parameters):
    with pytest.raises(RuntimeError):
        runner.inspect_parameters(parameter_predictor(parameters), "cuda", "fp16")


def test_loader_local_only_explicit_device_dtype_default_activation(monkeypatch):
    model = SimpleNamespace(eval=Mock())
    factory = Mock(return_value=SimpleNamespace(model=model))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=factory))
    predictor = runner.load_predictor("absolute-local-snapshot", "cuda", "fp16", fake_torch(True))
    factory.assert_called_once_with(
        "absolute-local-snapshot", device="cuda:0", local_files_only=True, trust_remote_code=False,
        model_kwargs={"torch_dtype": "torch.float16", "local_files_only": True})
    model.eval.assert_called_once_with()
    assert predictor.model is model
    assert "activation_fn" not in factory.call_args.kwargs


def score_row(query_id="q", scores=None):
    return {"query_id": query_id, "scored": scores or {"a": 1.0, "b": 2.0, "c": 3.0},
            "order": ["c", "b", "a"]}


def test_spearman_one_is_not_numeric_equality():
    row = runner.compare_score_rows([score_row()], [score_row(scores={"a": 11.0, "b": 12.0, "c": 13.0})])[0]
    assert row["spearman"] == pytest.approx(1)
    assert row["scores_exactly_equal"] is False
    assert row["max_abs_score_delta"] == 10
    assert row["order_match"] is True
    assert runner.compare_score_rows([score_row()], [score_row()])[0]["scores_exactly_equal"] is True


@pytest.mark.parametrize("candidate", [[], [score_row("other")], [score_row(), score_row()],
                                       [score_row(scores={"a": 1})], [score_row(scores={"a": math.nan, "b": 2, "c": 3})]])
def test_comparison_refuses_incomplete_misaligned_evidence(candidate):
    with pytest.raises(ValueError):
        runner.compare_score_rows([score_row()], candidate)


def test_cli_defaults_and_help():
    args = runner.parser().parse_args(["--inputs", "input.json", "--output", "out.json",
                                       "--device", "cpu", "--dtype", "fp32"])
    assert (args.timing_queries, args.timing_repeats, args.quality_queries, args.warmups) == (12, 2, 60, 3)
    assert "--skip-quality" in runner.parser().format_help()


def test_main_rejects_input_overwrite_before_loading(tmp_path):
    path = tmp_path / "input.json"
    with pytest.raises(ValueError, match="overwrite frozen inputs"):
        runner.main(["--inputs", str(path), "--output", str(path), "--device", "cpu", "--dtype", "fp32"])


def test_main_writes_raw_evidence_cpu_memory_null_without_real_model(inputs, tmp_path, monkeypatch):
    from contextlib import nullcontext

    input_path, output_path = tmp_path / "inputs.json", tmp_path / "report.json"
    input_path.write_text(json.dumps(inputs), encoding="utf-8")
    predictor = FakePredictor()
    predictor.model = SimpleNamespace(parameters=lambda: iter([parameter("torch.float32", "cpu")]),
                                      config=SimpleNamespace(_attn_implementation="eager"))
    predictor.activation_fn = SimpleNamespace()
    torch = fake_torch(False)
    torch.__version__ = "fake+cpu"
    torch.version = SimpleNamespace(cuda=None)
    torch.get_num_threads = lambda: 4
    torch.get_num_interop_threads = lambda: 2
    torch.get_float32_matmul_precision = lambda: "highest"
    torch.backends.cudnn.version = lambda: None
    torch.inference_mode = nullcontext
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(runner, "load_predictor", lambda *args: predictor)
    monkeypatch.setattr(runner, "_git", lambda *args: "unknown")
    assert runner.main(["--inputs", str(input_path), "--output", str(output_path),
                        "--device", "cpu", "--dtype", "fp32", "--quality-queries", "12"]) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metadata"]["verified_dtype"] == "fp32"
    assert report["metadata"]["versions"]["torch_runtime"] == "fake+cpu"
    assert report["metadata"]["peak_allocated_bytes"] is None
    assert report["metadata"]["peak_reserved_bytes"] is None
    assert report["metadata"]["cuda"]["gpu"] is None
    assert report["metadata"]["attention_implementation"] == "eager"
    assert report["metadata"]["input_metadata"] == inputs["metadata"]
    assert report["settings"]["predict_batch_size"] == 9
    assert report["cold_model_load_ms"] >= 0
    assert report["first_prediction_ms"] >= 0
    assert len(report["timing"]["rows"]) == 24
    assert len(report["quality"]["rows"]) == 12
    assert json.loads(input_path.read_text(encoding="utf-8")) == inputs


def test_offline_flags_are_forced(monkeypatch):
    import importlib
    import os

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    importlib.reload(runner)
    assert os.environ["HF_HUB_OFFLINE"] == os.environ["TRANSFORMERS_OFFLINE"] == "1"
