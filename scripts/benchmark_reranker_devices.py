"""Offline-only CPU/CUDA CrossEncoder benchmark over frozen fused candidates.

Run from the project root (or use ``python -m scripts.benchmark_reranker_devices``).
No retrieval, embedding, API, model download, or acceptance-gate decision occurs
here. Scores use CrossEncoder's unmodified production-default activation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import struct
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Set before importing any HF libraries; never respect an online override.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL_ID = "BAAI/bge-reranker-v2-m3"
BATCH_SIZE = 9


def _require_fields(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = set(fields) - value.keys()
    if missing:
        raise ValueError(f"{label} missing fields: {sorted(missing)}")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _reject_unc_path(path: str | Path, label: str) -> None:
    """Reject explicit UNC/device paths before any resolve/stat/read operation."""
    if str(path).replace("\\", "/").startswith("//"):
        raise ValueError(f"{label} must not use a UNC/network path")


def _validate_weight_file(path: Path) -> None:
    """Reject empty/placeholder weights without deserializing tensors.

    The real loader remains the final authority on tensor contents/architecture.
    Safetensors offsets must cover a nonempty payload. For PyTorch weights we
    accept the current ZIP serialization, not arbitrary pickle executables.
    """
    if not path.is_file():
        raise ValueError(f"missing local weights: {path}")
    if path.suffix == ".safetensors":
        with path.open("rb") as handle:
            prefix = handle.read(8)
            if len(prefix) != 8:
                raise ValueError(f"invalid safetensors weights: {path}")
            header_size = struct.unpack("<Q", prefix)[0]
            if not 2 <= header_size <= min(16 * 1024 * 1024, path.stat().st_size - 8):
                raise ValueError(f"invalid safetensors header: {path}")
            try:
                header = json.loads(handle.read(header_size))
            except (ValueError, UnicodeError) as error:
                raise ValueError(f"invalid safetensors header: {path}") from error
        if not isinstance(header, dict):
            raise ValueError(f"invalid safetensors header: {path}")
        ranges = []
        for name, tensor in header.items():
            if name == "__metadata__":
                continue
            if not isinstance(tensor, dict) or not isinstance(tensor.get("shape"), list):
                raise ValueError(f"invalid tensor header: {path}")
            offsets = tensor.get("data_offsets")
            if (not isinstance(offsets, list) or len(offsets) != 2
                    or any(type(n) is not int for n in offsets)
                    or not 0 <= offsets[0] <= offsets[1]):
                raise ValueError(f"invalid tensor offsets: {path}")
            ranges.append(offsets)
        ranges.sort()
        cursor = 0
        for start, end in ranges:
            if start != cursor:
                raise ValueError(f"noncontiguous tensor payload: {path}")
            cursor = end
        if not ranges or cursor <= 0 or cursor != path.stat().st_size - 8 - header_size:
            raise ValueError(f"missing/truncated tensor payload: {path}")
    elif path.suffix == ".bin":
        if not zipfile.is_zipfile(path):
            raise ValueError(f"local PyTorch weights must use ZIP serialization: {path}")
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not any(m.filename.endswith("/data.pkl") and m.file_size for m in members) or not any(
                "/data/" in m.filename and m.file_size for m in members
            ):
                raise ValueError(f"missing tensor payload in PyTorch weights: {path}")
    else:
        raise ValueError(f"unsupported weight file: {path}")


def validate_snapshot(path_value: Any) -> dict[str, Any]:
    _text(path_value, "model_path")
    _reject_unc_path(path_value, "model_path")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("model_path must be an existing absolute local snapshot directory")
    config_path = path / "config.json"
    if not config_path.is_file():
        raise ValueError("local snapshot lacks config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid local model config.json") from error
    if not isinstance(config, dict) or not config.get("model_type"):
        raise ValueError("local model config lacks model_type")
    weights: list[Path] = []
    for stem in ("model.safetensors", "pytorch_model.bin"):
        if (path / stem).is_file():
            weights = [path / stem]
            break
        index = path / f"{stem}.index.json"
        if index.is_file():
            manifest = json.loads(index.read_text(encoding="utf-8"))
            mapping = manifest.get("weight_map") if isinstance(manifest, dict) else None
            if not isinstance(mapping, dict) or not mapping:
                raise ValueError(f"invalid weight shard index: {index}")
            for filename in mapping.values():
                if not isinstance(filename, str) or not filename or Path(filename).name != filename:
                    raise ValueError(f"invalid shard filename: {filename!r}")
            weights = [path / filename for filename in sorted(set(mapping.values()))]
            break
    if not weights:
        raise ValueError("local snapshot lacks real model weights")
    for weight in weights:
        _validate_weight_file(weight)
    return {
        "path": str(path),
        # Preserve the snapshot name, not the resolved blob symlink target.
        "revision": path.name if path.parent.name == "snapshots" else "unknown",
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "weights": [{"name": p.name, "bytes": p.stat().st_size} for p in weights],
    }


def validate_inputs(data: Any) -> dict[str, Any]:
    _require_fields(data, ("schema_version", "model_id", "model_path", "max_chars",
                           "production_pairs", "quality_pairs", "metadata", "batches"), "inputs")
    for key, expected in (("schema_version", 1), ("max_chars", 768),
                          ("production_pairs", 9), ("quality_pairs", 20)):
        if type(data[key]) is not int or data[key] != expected:
            raise ValueError(f"inputs.{key} must be {expected}")
    if data["model_id"] != MODEL_ID:
        raise ValueError(f"model_id must be {MODEL_ID}")
    # These provenance names match the frozen-input producer; retain all extra
    # evidence verbatim, but do not silently accept incomplete provenance.
    metadata = data["metadata"]
    _require_fields(metadata, ("generated_at", "benchmark_version", "benchmark_root",
                               "artifact_root", "source_hashes"), "inputs.metadata")
    for field in ("generated_at", "benchmark_version", "benchmark_root", "artifact_root"):
        _text(metadata[field], f"inputs.metadata.{field}")
    hashes = metadata["source_hashes"]
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("inputs.metadata.source_hashes must be nonempty")
    for source, digest in hashes.items():
        _text(source, "source_hashes source")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdefABCDEF" for c in digest)):
            raise ValueError(f"invalid source SHA256: {source}")
    batches = data["batches"]
    if not isinstance(batches, list) or len(batches) != 60:
        raise ValueError("inputs.batches must contain exactly 60 frozen queries")
    query_ids: set[str] = set()
    domains = []
    for position, batch in enumerate(batches):
        label = f"batches[{position}]"
        _require_fields(batch, ("query_id", "domain", "query", "candidates", "qrels"), label)
        for field in ("query_id", "domain", "query"):
            _text(batch[field], f"{label}.{field}")
        if batch["query_id"] in query_ids:
            raise ValueError(f"duplicate query_id: {batch['query_id']}")
        query_ids.add(batch["query_id"])
        domains.append(batch["domain"])
        candidates = batch["candidates"]
        if not isinstance(candidates, list) or len(candidates) != 20:
            raise ValueError(f"{label} needs exactly 20 quality candidates (first 9 for timing)")
        chunk_ids: set[str] = set()
        for candidate in candidates:
            _require_fields(candidate, ("chunk_id", "document", "original_chars", "truncated_chars"), label)
            _text(candidate["chunk_id"], f"{label}.chunk_id")
            _text(candidate["document"], f"{label}.document")
            if candidate["chunk_id"] in chunk_ids:
                raise ValueError(f"duplicate chunk_id in {label}: {candidate['chunk_id']}")
            chunk_ids.add(candidate["chunk_id"])
            original, truncated = candidate["original_chars"], candidate["truncated_chars"]
            if (type(original) is not int or type(truncated) is not int
                    or not 0 < truncated <= original or truncated != len(candidate["document"])
                    or truncated > data["max_chars"]):
                raise ValueError(f"invalid original/truncated character counts in {label}")
        qrels = batch["qrels"]
        if not isinstance(qrels, dict) or not qrels:
            raise ValueError(f"{label}.qrels must contain ordinary chunk ID relevance")
        for chunk_id, grade in qrels.items():
            _text(chunk_id, f"{label}.qrels chunk_id")
            if type(grade) is not int or grade < 0:
                raise ValueError(f"invalid qrel grade in {label}")
        if not any(grade > 0 for grade in qrels.values()):
            raise ValueError(f"{label}.qrels has no relevant chunk")
        # Relevant IDs absent from the candidate set MUST stay in the denominator.
    if len(set(domains)) != 2 or any(domains[i] != domains[i % 2] for i in range(60)):
        raise ValueError("batches must alternate between exactly two domains")
    return validate_snapshot(data["model_path"])


def load_inputs(path: Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    _reject_unc_path(path, "inputs")
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    snapshot = validate_inputs(data)
    return data, hashlib.sha256(raw).hexdigest(), snapshot


def nearest_rank(values: list[float], percent: float) -> float:
    if not values or not 0 < percent <= 100:
        raise ValueError("nearest-rank needs samples and 0 < percent <= 100")
    return sorted(values)[math.ceil(len(values) * percent / 100) - 1]


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {"count": len(values), "p50": nearest_rank(values, 50),
            "p95": nearest_rank(values, 95), "mean": statistics.fmean(values),
            "max": max(values), "total": sum(values), "percentile_method": "nearest-rank"}


def rank_scores(candidates: list[dict[str, Any]], scores: list[float]) -> list[str]:
    if len(candidates) != len(scores) or any(not math.isfinite(s) for s in scores):
        raise ValueError("predict must return one finite scalar score per candidate")
    return [c["chunk_id"] for c, _ in sorted(
        zip(candidates, scores), key=lambda item: (-item[1], item[0]["chunk_id"]))]


def validate_options(args: argparse.Namespace, batch_count: int = 60) -> None:
    if args.device not in ("cpu", "cuda") or args.dtype not in ("fp32", "fp16"):
        raise ValueError("unsupported device/dtype")
    if args.device == "cpu" and args.dtype == "fp16":
        raise ValueError("CPU fp16 is forbidden; use CPU fp32")
    for name in ("timing_queries", "quality_queries"):
        if not 1 <= getattr(args, name) <= batch_count:
            raise ValueError(f"{name} must be between 1 and {batch_count}")
    if args.timing_repeats < 1 or args.warmups < 0 or (args.threads is not None and args.threads < 1):
        raise ValueError("repeats/threads must be positive; warmups must be nonnegative")


def configure_torch(torch: Any, args: argparse.Namespace) -> None:
    validate_options(args)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable; CPU fallback is forbidden")
    if args.threads is not None:
        torch.set_num_threads(args.threads)
    if args.device == "cuda":
        torch.cuda.set_device(0)
        torch.set_float32_matmul_precision("highest")
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.synchronize(0)
        torch.cuda.reset_peak_memory_stats(0)


def inspect_parameters(predictor: Any, device: str, dtype: str) -> dict[str, Any]:
    parameters = list(predictor.model.parameters())
    if not parameters:
        raise RuntimeError("CrossEncoder has no parameters")
    floating = [p for p in parameters if p.is_floating_point()]
    dtype_counts = Counter(str(p.dtype) for p in floating)
    devices = Counter(str(p.device) for p in parameters)
    expected = "torch.float32" if dtype == "fp32" else "torch.float16"
    if not floating or set(dtype_counts) != {expected}:
        raise RuntimeError(f"requested {dtype} but model parameter dtypes are {dict(dtype_counts)}")
    expected_devices = {"cpu"} if device == "cpu" else {"cuda:0"}
    if set(devices) != expected_devices:
        raise RuntimeError(f"requested {device} but model parameter devices are {dict(devices)}")
    return {"floating_parameter_dtypes": dict(dtype_counts), "parameter_devices": dict(devices),
            "parameter_count": sum(p.numel() for p in parameters), "verified_dtype": dtype}


def load_predictor(model_path: str, device: str, dtype: str, torch: Any) -> Any:
    from sentence_transformers import CrossEncoder

    predictor = CrossEncoder(
        model_path, device="cuda:0" if device == "cuda" else "cpu",
        local_files_only=True, trust_remote_code=False,
        model_kwargs={"torch_dtype": torch.float16 if dtype == "fp16" else torch.float32,
                      "local_files_only": True},
    )
    predictor.model.eval()
    # No activation override: identical default to CrossEncoderReranker.
    return predictor


def predict_batch(predictor: Any, batch: dict[str, Any], count: int,
                  sync: Callable[[], None] = lambda: None,
                  clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    candidates = batch["candidates"][:count]
    pairs = [(batch["query"], candidate["document"]) for candidate in candidates]
    sync()
    started = clock()
    raw = predictor.predict(pairs, batch_size=BATCH_SIZE, show_progress_bar=False,
                            convert_to_numpy=True)
    sync()
    elapsed = (clock() - started) * 1000
    # Scalar conversion/sorting/metrics are outside the inference timing window.
    scores = [float(score) for score in raw]
    order = rank_scores(candidates, scores)
    return {"query_id": batch["query_id"], "domain": batch["domain"], "ms": elapsed,
            "pairs": count, "candidate_ids": [c["chunk_id"] for c in candidates],
            "scored": {c["chunk_id"]: s for c, s in zip(candidates, scores)}, "order": order}


def run_predictions(predictor: Any, data: dict[str, Any], args: argparse.Namespace,
                    sync: Callable[[], None] = lambda: None,
                    clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    from rag.evaluation.metrics import aggregate_metrics, evaluate_ranking

    validate_options(args, len(data["batches"]))
    batches = data["batches"]
    first = predict_batch(predictor, batches[0], 9, sync, clock)
    warmups = [predict_batch(predictor, batches[i % len(batches)], 9, sync, clock)
               for i in range(args.warmups)]
    timing = []
    for repeat in range(args.timing_repeats):
        for query_index, batch in enumerate(batches[:args.timing_queries]):
            row = predict_batch(predictor, batch, 9, sync, clock)
            row.update({"repeat": repeat + 1, "query_index": query_index})
            timing.append(row)
    quality = []
    if not args.skip_quality:
        for batch in batches[:args.quality_queries]:
            row = predict_batch(predictor, batch, 20, sync, clock)
            row["qrels"] = dict(batch["qrels"])
            row["metrics"] = evaluate_ranking(row["order"], batch["qrels"], cutoffs=(10,))
            quality.append(row)
    return {
        "first_prediction_ms": first["ms"], "first_prediction": first,
        "warmups": {"count": len(warmups), "included_in_timing": False, "rows": warmups},
        "timing": {"latency_ms": latency_summary([r["ms"] for r in timing]),
                   "by_domain": {domain: latency_summary([r["ms"] for r in timing if r["domain"] == domain])
                                 for domain in sorted({r["domain"] for r in timing})},
                   "rows": timing},
        "quality": {"skipped": args.skip_quality, "scoring_mode": "ordinary_chunk_id",
                    "count": len(quality), "overall": aggregate_metrics(r["metrics"] for r in quality),
                    "by_domain": {domain: aggregate_metrics(r["metrics"] for r in quality if r["domain"] == domain)
                                  for domain in sorted({r["domain"] for r in quality})},
                    "rows": quality},
        "acceptance_gate": {"status": "not_evaluated", "reason": "single-mode evidence only"},
    }


def compare_score_rows(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strict optional comparison helper. Rank correlation is NOT score equality."""
    from scripts.benchmark_reranker_backends import spearman

    def indexed(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result = {r["query_id"]: r for r in rows}
        if not rows or len(result) != len(rows):
            raise ValueError("comparison needs nonempty, unique query IDs")
        return result

    left, right = indexed(reference), indexed(candidate)
    if left.keys() != right.keys():
        raise ValueError("comparison query sets differ")
    results = []
    for query_id, ref in left.items():
        cand = right[query_id]
        if not ref["scored"] or ref["scored"].keys() != cand["scored"].keys():
            raise ValueError(f"comparison candidate sets differ: {query_id}")
        ids = sorted(ref["scored"])
        a, b = [ref["scored"][c] for c in ids], [cand["scored"][c] for c in ids]
        if any(not math.isfinite(x) for x in a + b):
            raise ValueError("comparison scores must be finite")
        results.append({"query_id": query_id, "spearman": spearman(a, b),
                        "scores_exactly_equal": a == b,
                        "max_abs_score_delta": max(abs(x - y) for x, y in zip(a, b)),
                        "top1_match": ref["order"][:1] == cand["order"][:1],
                        "top3_match": ref["order"][:3] == cand["order"][:3],
                        "order_match": ref["order"] == cand["order"]})
    return results


def _git(*arguments: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), *arguments],
                                       stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def runtime_metadata(torch: Any, predictor: Any, args: argparse.Namespace) -> dict[str, Any]:
    versions = {}
    for package in ("torch", "sentence-transformers", "transformers", "numpy", "safetensors", "tokenizers"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unknown"
    versions["torch_runtime"] = str(torch.__version__)
    activation = getattr(predictor, "activation_fn", None)
    if activation is None:
        activation = getattr(predictor, "default_activation_function", None)
    config = predictor.model.config
    gpu = None
    if args.device == "cuda":
        props = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        gpu = {"index": 0, "name": props.name, "compute_capability": [props.major, props.minor],
               "total_memory_bytes": props.total_memory, "free_memory_bytes_at_report": free,
               "memory_total_bytes_at_report": total,
               "peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
               "peak_reserved_bytes": torch.cuda.max_memory_reserved(0)}
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "versions": versions, "python": sys.version, "executable": sys.executable,
        "platform": platform.platform(), "processor": platform.processor(),
        "git": {"commit": _git("rev-parse", "HEAD"), "status_porcelain": _git("status", "--porcelain")},
        "torch_threads": torch.get_num_threads(), "torch_interop_threads": torch.get_num_interop_threads(),
        "device": args.device, "dtype": args.dtype,
        "tf32": {"cuda_matmul_allow_tf32": torch.backends.cuda.matmul.allow_tf32,
                 "cudnn_allow_tf32": torch.backends.cudnn.allow_tf32,
                 "float32_matmul_precision": torch.get_float32_matmul_precision()},
        "attention_implementation": getattr(config, "_attn_implementation", "unknown"),
        "activation": {"policy": "unmodified CrossEncoder default (production parity)",
                       "class": f"{type(activation).__module__}.{type(activation).__qualname__}" if activation is not None else "unknown",
                       "repr": repr(activation)},
        "cuda": {"torch_build_version": torch.version.cuda, "available": torch.cuda.is_available(),
                 "cudnn_version": torch.backends.cudnn.version(), "gpu": gpu},
        "peak_allocated_bytes": gpu["peak_allocated_bytes"] if gpu else None,
        "peak_reserved_bytes": gpu["peak_reserved_bytes"] if gpu else None,
        "offline": {name: os.environ[name] for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")},
        "agent_configuration": {"requested": {"tool": "functions.Agent", "provider": "openai-codex",
                                               "model": "gpt-6.1-sol", "reasoning": "high"},
                                "observed_environment": {"provider": os.getenv("PI_PROVIDER", "unknown"),
                                                         "model": os.getenv("PI_MODEL", "unknown"),
                                                         "reasoning": os.getenv("PI_REASONING_LEVEL", "unknown")},
                                "effective_tool": "unknown"},
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inputs", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--device", choices=("cpu", "cuda"), required=True)
    result.add_argument("--dtype", choices=("fp32", "fp16"), required=True)
    result.add_argument("--timing-queries", type=int, default=12)
    result.add_argument("--timing-repeats", type=int, default=2)
    result.add_argument("--quality-queries", type=int, default=60)
    result.add_argument("--threads", type=int, default=None, help="torch intra-op threads; default retains runtime setting")
    result.add_argument("--warmups", type=int, default=3)
    result.add_argument("--skip-quality", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    validate_options(args)
    _reject_unc_path(args.inputs, "inputs")
    _reject_unc_path(args.output, "output")
    if args.inputs.resolve() == args.output.resolve():
        raise ValueError("output must not overwrite frozen inputs")
    data, input_sha, snapshot = load_inputs(args.inputs)
    import torch

    configure_torch(torch, args)
    sync = (lambda: torch.cuda.synchronize(0)) if args.device == "cuda" else (lambda: None)
    sync()
    started = time.perf_counter()
    predictor = load_predictor(data["model_path"], args.device, args.dtype, torch)
    sync()
    cold_ms = (time.perf_counter() - started) * 1000
    parameter_info = inspect_parameters(predictor, args.device, args.dtype)
    with torch.inference_mode():
        report = run_predictions(predictor, data, args, sync)
    # Re-check after inference too: a predictor must not secretly recast/offload.
    parameter_info = inspect_parameters(predictor, args.device, args.dtype)
    report.update({"schema_version": 1, "benchmark": "offline_reranker_devices",
                   "cold_model_load_ms": cold_ms,
                   "metadata": {**runtime_metadata(torch, predictor, args), **parameter_info,
                                "model_id": data["model_id"], "model_snapshot": snapshot,
                                "inputs_path": str(args.inputs.resolve()), "input_sha256": input_sha,
                                "input_metadata": data["metadata"]},
                   "settings": {"device": args.device, "dtype": args.dtype, "predict_batch_size": BATCH_SIZE,
                                "max_chars": 768, "production_pairs": 9, "quality_pairs": 20,
                                "timing_queries": args.timing_queries, "timing_repeats": args.timing_repeats,
                                "quality_queries": 0 if args.skip_quality else args.quality_queries,
                                "warmups": args.warmups, "requested_threads": args.threads,
                                "query_selection": "frozen input prefix, unchanged order",
                                "timing_scope": "predict including tokenization, device transfer, output conversion; synchronized CUDA",
                                "peak_memory_scope": "since before model load, including first prediction, warmups, timing and quality"}})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"wrote {args.output} ({args.device}/{args.dtype}; gates not evaluated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
