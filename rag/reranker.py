"""Injectable Cross-Encoder reranking with an offline deterministic fake."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from .build import _terms
from .model_devices import (
    RERANK_DEVICE_ENV,
    device_from_env,
    disable_tf32,
    require_device_available,
    rerank_dtype_from_env,
    verify_and_log_model,
)
from .models import RetrievalHit

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

#: Cross-Encoder cost is linear in the padded input length, and `predict()`
#: pads a whole batch to its longest pair — so the longest document in the
#: candidate set sets the price for every pair in it. Clipping each pair's
#: document side is therefore the cheapest lever on rerank latency that does
#: not touch retrieval. `0` disables clipping (the pre-optimization shape).
#:
#: 768 is a measured choice, not a guess. The plan proposed 512; on the 60-query
#: RAG benchmark 512 dropped `hybrid_rerank` Recall@10 by 1.39pt, over the 1pt
#: red line, so the plan's own "retest with a larger budget" branch was taken.
#: 768 clears all three gate items (Recall@10 +0.83pt, MRR -0.55%, nDCG -0.93%,
#: all within tolerance) and cuts rerank P95 by 42% — but only 3% off P50,
#: because it leaves every median-sized batch untouched. It is a tail fix.
RERANK_MAX_CHARS_ENV = "SMARTCS_RERANK_MAX_CHARS"
RERANK_MAX_CHARS_DEFAULT = 768

#: How far back to look for a whitespace boundary before cutting at the exact
#: character limit. Bounded so a long CJK run — which has no whitespace to
#: align to at all — does not collapse to some far-away space.
_WORD_BOUNDARY_LOOKBACK = 32
_WORD_SEPARATORS = " \t\n\r\f\v"


def max_chars_from_env(value: str | None = None) -> int:
    """Read the rerank truncation budget, refusing a value we cannot honour."""

    if value is None:
        value = os.getenv(RERANK_MAX_CHARS_ENV)
    if value is None or not value.strip():
        return RERANK_MAX_CHARS_DEFAULT
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{RERANK_MAX_CHARS_ENV} must be an integer, got {value!r}") from error
    if parsed < 0:
        raise ValueError(f"{RERANK_MAX_CHARS_ENV} must be >= 0, got {parsed}")
    return parsed


def truncate_for_rerank(text: str, max_chars: int) -> str:
    """Clip one pair's document side, aligned to a word boundary where one exists.

    Slicing a `str` slices code points, so the result can never hold half of a
    UTF-8 sequence. Latin text backs off to the previous whitespace within
    `_WORD_BOUNDARY_LOOKBACK`; CJK has no whitespace to align to and is cut at
    the character limit, which is what its readers expect anyway.
    """

    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    boundary = max(head.rfind(separator) for separator in _WORD_SEPARATORS)
    if boundary > 0 and boundary >= max_chars - _WORD_BOUNDARY_LOOKBACK:
        return head[:boundary]
    return head


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        ...


class FakeReranker:
    """Deterministic local reranker for tests; it never downloads a model."""

    backend_name = "fake"
    model_name = "fake"

    def __init__(self, score_fn: Callable[[str, RetrievalHit], float] | None = None):
        self.score_fn = score_fn

    def _score(self, query: str, candidate: RetrievalHit) -> float:
        if self.score_fn is not None:
            return float(self.score_fn(query, candidate))
        query_terms = set(_terms(query))
        content_terms = set(_terms(candidate.retrieval_text or candidate.content))
        return float(len(query_terms & content_terms))

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        scored = []
        for position, value in enumerate(candidates):
            candidate = RetrievalHit.from_value(value)
            candidate.rerank_score = self._score(query, candidate)
            scored.append((candidate.rerank_score, position, candidate))
        scored.sort(key=lambda item: (-item[0], item[1], item[2].chunk_id))
        result = [item[2] for item in scored[: max(0, top_k)]]
        for rank, candidate in enumerate(result, start=1):
            candidate.rank = rank
            candidate.score = float(candidate.rerank_score or 0.0)
        return result


DeterministicFakeReranker = FakeReranker


def _document_of(item: RetrievalHit) -> str:
    """The text a pair is scored on: contextualized text, else raw content."""

    candidate = RetrievalHit.from_value(item)
    return candidate.retrieval_text or candidate.content


def _rank_by_score(
    scores: Iterable[float], candidates: list[RetrievalHit], top_k: int
) -> list[RetrievalHit]:
    """Shared ranking tail, so every backend orders and stamps hits identically.

    Ties break on `chunk_id`, never on input order — the candidate list comes
    from RRF and must not leak its ordering into the result.
    """

    ranked = sorted(
        zip(scores, candidates), key=lambda item: (-float(item[0]), item[1].chunk_id)
    )[: max(0, top_k)]
    result = []
    for rank, (score, value) in enumerate(ranked, start=1):
        candidate = RetrievalHit.from_value(value)
        candidate.rerank_score = float(score)
        candidate.score = float(score)
        candidate.rank = rank
        result.append(candidate)
    return result


class CrossEncoderReranker:
    """Production Cross-Encoder wrapper; model loading stays explicit and injectable."""

    backend_name = "sentence_transformers"

    def __init__(
        self,
        model_name: str = RERANKER_MODEL,
        model=None,
        *,
        max_chars: int | None = None,
    ):
        self.model_name = model_name
        self.max_chars = max_chars_from_env() if max_chars is None else int(max_chars)
        if self.max_chars < 0:
            raise ValueError(f"max_chars must be >= 0, got {self.max_chars}")
        device = device_from_env(RERANK_DEVICE_ENV)
        dtype = rerank_dtype_from_env(device)
        require_device_available(device)
        injected = model is not None
        if not injected:
            import torch
            from sentence_transformers import CrossEncoder

            if dtype == "fp32" and (device == "cuda:0" or (device is None and torch.cuda.is_available())):
                disable_tf32(torch)
            # Load directly at the requested precision, rather than allocating
            # FP32 CPU weights first and converting a second copy on the GPU.
            model_kwargs = {"torch_dtype": torch.float16 if dtype == "fp16" else torch.float32}
            if device == "cuda:0":
                model_kwargs["device_map"] = {"": device}
            # sentence-transformers 3.x called this argument automodel_args.
            parameters = inspect.signature(CrossEncoder).parameters
            argument = (
                "automodel_args"
                if "automodel_args" in parameters and "model_kwargs" not in parameters
                else "model_kwargs"
            )
            kwargs = {argument: model_kwargs}
            if device is not None:
                kwargs["device"] = device
            model = CrossEncoder(model_name, **kwargs)
        verify_and_log_model(model, model_name, device, dtype, injected=injected)
        self._model = model

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        if not candidates:
            return []
        pairs = [
            (query, truncate_for_rerank(_document_of(item), self.max_chars)) for item in candidates
        ]
        return _rank_by_score(self._model.predict(pairs), candidates, top_k)


#: Where `scripts/export_reranker_onnx.py` writes by default.
RERANK_ONNX_DIR_ENV = "SMARTCS_RERANK_ONNX_DIR"
RERANK_ONNX_DIR_DEFAULT = "artifacts/reranker_onnx"


class OnnxReranker:
    """Opt-in int8 ONNX Cross-Encoder; same contract as `CrossEncoderReranker`.

    It exists because the CPU Cross-Encoder dominates retrieval latency (Phase
    11: rerank was 95% of it). It is not a different ranking model — it runs the
    same weights, dynamically quantized to int8.

    **Measured verdict (Phase 12), which is why it stays opt-in and is not the
    default:** on the same batches it is ~2x faster than the float backend, but
    dynamic quantization moves the sigmoid scores enough to reorder candidates
    (Spearman as low as 0.88 per batch, against a 0.95 acceptance bar). See
    `pi-harness/PHASE12_REPORT.md`. Its fp32 graph, by contrast, reproduces the
    float backend's scores exactly (Spearman 1.0000), which is what pins the
    disagreement on quantization rather than on this loader.
    """

    backend_name = "onnx_int8"

    def __init__(
        self,
        model_name: str = RERANKER_MODEL,
        *,
        model_dir: str | Path | None = None,
        max_chars: int | None = None,
        session=None,
        tokenizer=None,
    ):
        import json

        self.model_name = model_name
        self.max_chars = max_chars_from_env() if max_chars is None else int(max_chars)
        if self.max_chars < 0:
            raise ValueError(f"max_chars must be >= 0, got {self.max_chars}")
        self.model_dir = Path(
            model_dir if model_dir is not None else os.getenv(RERANK_ONNX_DIR_ENV) or RERANK_ONNX_DIR_DEFAULT
        )
        manifest_path = self.model_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"no ONNX reranker artifact at {manifest_path}; "
                f"run `python -m scripts.export_reranker_onnx` first"
            )
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.max_tokens = int(self.manifest.get("max_tokens") or 512)

        if tokenizer is None:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(self.manifest.get("model_id") or model_name)
        self._tokenizer = tokenizer

        if session is None:
            import onnxruntime

            graph = self.model_dir / "reranker_int8.onnx"
            if not graph.is_file():
                raise FileNotFoundError(f"ONNX graph missing: {graph}")
            options = onnxruntime.SessionOptions()
            threads = os.getenv("SMARTCS_RERANK_ONNX_THREADS")
            if threads and threads.strip():
                options.intra_op_num_threads = int(threads)
            session = onnxruntime.InferenceSession(
                str(graph), sess_options=options, providers=["CPUExecutionProvider"]
            )
        self._session = session

    def _score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        import numpy as np

        encoded = self._tokenizer(
            [query for query, _ in pairs],
            [document for _, document in pairs],
            padding=True,
            truncation=True,
            max_length=self.max_tokens,
            return_tensors="np",
        )
        logits = self._session.run(
            ["logits"],
            {
                "input_ids": encoded["input_ids"].astype(np.int64),
                "attention_mask": encoded["attention_mask"].astype(np.int64),
            },
        )[0]
        # Same activation the sentence-transformers CrossEncoder applies for a
        # single-label head, so `hit.score` stays comparable across backends.
        return [float(1.0 / (1.0 + np.exp(-float(row[0])))) for row in logits]

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        if not candidates:
            return []
        pairs = [
            (query, truncate_for_rerank(_document_of(item), self.max_chars)) for item in candidates
        ]
        return _rank_by_score(self._score_pairs(pairs), candidates, top_k)


def create_reranker(
    backend: str = "fake",
    *,
    model_name: str = RERANKER_MODEL,
    model=None,
    max_chars: int | None = None,
    model_dir: str | Path | None = None,
) -> Reranker:
    name = backend.strip().lower()
    if name in {"fake", "hash", "dry_run"}:
        return FakeReranker()
    if name in {"cross_encoder", "sentence_transformers", "local"}:
        return CrossEncoderReranker(model_name=model_name, model=model, max_chars=max_chars)
    if name in {"onnx_int8", "onnx"}:
        return OnnxReranker(model_name=model_name, model_dir=model_dir, max_chars=max_chars)
    raise ValueError(f"unsupported reranker backend: {backend}")


__all__ = [
    "CrossEncoderReranker",
    "DeterministicFakeReranker",
    "FakeReranker",
    "OnnxReranker",
    "RERANKER_MODEL",
    "RERANK_MAX_CHARS_DEFAULT",
    "RERANK_MAX_CHARS_ENV",
    "RERANK_ONNX_DIR_DEFAULT",
    "RERANK_ONNX_DIR_ENV",
    "Reranker",
    "create_reranker",
    "max_chars_from_env",
    "truncate_for_rerank",
]
