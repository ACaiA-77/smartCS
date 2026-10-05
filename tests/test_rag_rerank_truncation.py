"""Phase 12: rerank input truncation (the CPU Cross-Encoder cost lever)."""

from __future__ import annotations

import json

import pytest

from rag.models import RetrievalHit
from rag.reranker import (
    RERANK_MAX_CHARS_DEFAULT,
    RERANK_MAX_CHARS_ENV,
    RERANKER_MODEL,
    CrossEncoderReranker,
    OnnxReranker,
    create_reranker,
    max_chars_from_env,
    truncate_for_rerank,
)


class RecordingCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder; records the pairs."""

    def __init__(self, score=1.0):
        self.pairs = []
        self._score = score

    def predict(self, pairs):
        self.pairs = list(pairs)
        return [self._score for _ in pairs]


def _candidate(text: str, chunk_id: str = "chunk-1") -> RetrievalHit:
    return RetrievalHit(
        chunk_id,
        "agent_engineering",
        1,
        0.1,
        "guide.md",
        "content-fallback",
        retrieval_text=text,
    )


# --- truncate_for_rerank -----------------------------------------------------


def test_text_within_budget_is_returned_untouched():
    text = "short enough"
    assert truncate_for_rerank(text, 512) == text


def test_disabled_budget_returns_full_text():
    text = "x" * 5000
    assert truncate_for_rerank(text, 0) == text
    assert truncate_for_rerank(text, -1) == text


def test_latin_text_backs_off_to_the_previous_word_boundary():
    text = "alpha beta gamma delta"
    # 14 lands inside "gamma"; the last space inside the head is at index 10.
    assert truncate_for_rerank(text, 14) == "alpha beta"


def test_word_boundary_lookback_is_bounded_for_cjk_runs():
    # One space early, then a long CJK run: the cut must NOT collapse to it.
    text = "x" + "中" * 200
    clipped = truncate_for_rerank(text, 100)
    assert clipped == text[:100]
    assert len(clipped) == 100


def test_cjk_without_whitespace_is_cut_by_character():
    text = "苹果账户注册流程" * 100
    clipped = truncate_for_rerank(text, 50)
    assert clipped == text[:50]


def test_cut_never_splits_a_utf8_sequence_or_a_surrogate_pair():
    text = "🙂" * 100  # astral plane: two UTF-16 code units each
    clipped = truncate_for_rerank(text, 7)
    assert clipped == "🙂" * 7
    assert clipped.encode("utf-8").decode("utf-8") == clipped
    assert "�" not in clipped


def test_truncation_is_a_prefix_of_the_original():
    text = " ".join(f"word{index}" for index in range(500))
    clipped = truncate_for_rerank(text, 200)
    assert text.startswith(clipped)
    assert len(clipped) <= 200


# --- max_chars_from_env ------------------------------------------------------


def test_env_absent_or_blank_falls_back_to_the_documented_default(monkeypatch):
    monkeypatch.delenv(RERANK_MAX_CHARS_ENV, raising=False)
    assert RERANK_MAX_CHARS_DEFAULT == 768
    assert max_chars_from_env() == RERANK_MAX_CHARS_DEFAULT
    assert max_chars_from_env("") == RERANK_MAX_CHARS_DEFAULT
    assert max_chars_from_env("   ") == RERANK_MAX_CHARS_DEFAULT


def test_env_value_is_parsed_including_the_disable_value(monkeypatch):
    monkeypatch.setenv(RERANK_MAX_CHARS_ENV, "1024")
    assert max_chars_from_env() == 1024
    assert max_chars_from_env("0") == 0


def test_env_value_that_cannot_be_honoured_is_refused_not_silently_defaulted():
    with pytest.raises(ValueError, match=RERANK_MAX_CHARS_ENV):
        max_chars_from_env("five-hundred")
    with pytest.raises(ValueError, match=">= 0"):
        max_chars_from_env("-1")


# --- CrossEncoderReranker wiring ---------------------------------------------


def test_reranker_clips_document_but_never_the_query():
    long_document = " ".join(f"token{index}" for index in range(400))
    model = RecordingCrossEncoder()
    CrossEncoderReranker(model=model, max_chars=64).rerank("我的查询", [_candidate(long_document)], 1)
    query, document = model.pairs[0]
    assert query == "我的查询"
    assert len(document) <= 64
    assert long_document.startswith(document)


def test_reranker_with_clipping_disabled_sends_the_full_document():
    long_document = "y" * 4000
    model = RecordingCrossEncoder()
    CrossEncoderReranker(model=model, max_chars=0).rerank("query", [_candidate(long_document)], 1)
    assert model.pairs == [("query", long_document)]


def test_reranker_reads_the_budget_from_the_environment(monkeypatch):
    monkeypatch.setenv(RERANK_MAX_CHARS_ENV, "32")
    model = RecordingCrossEncoder()
    reranker = CrossEncoderReranker(model=model)
    assert reranker.max_chars == 32
    reranker.rerank("query", [_candidate("z" * 500)], 1)
    assert len(model.pairs[0][1]) == 32


def test_explicit_budget_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(RERANK_MAX_CHARS_ENV, "32")
    model = RecordingCrossEncoder()
    reranker = CrossEncoderReranker(model=model, max_chars=0)
    assert reranker.max_chars == 0
    reranker.rerank("query", [_candidate("z" * 500)], 1)
    assert len(model.pairs[0][1]) == 500


def test_negative_explicit_budget_is_refused():
    with pytest.raises(ValueError, match="max_chars"):
        CrossEncoderReranker(model=RecordingCrossEncoder(), max_chars=-5)


def test_create_reranker_forwards_the_budget(monkeypatch):
    monkeypatch.delenv(RERANK_MAX_CHARS_ENV, raising=False)
    reranker = create_reranker("cross_encoder", model=RecordingCrossEncoder(), max_chars=256)
    assert isinstance(reranker, CrossEncoderReranker)
    assert reranker.max_chars == 256


def test_create_reranker_still_refuses_an_unknown_backend():
    with pytest.raises(ValueError, match="unsupported reranker backend"):
        create_reranker("not_a_backend_this_repo_ever_shipped")


# --- truncation must not change the contract of rerank() ---------------------


def test_every_candidate_is_scored_once_and_ordering_is_unchanged_by_clipping():
    candidates = [
        _candidate(" ".join(f"a{index}" for index in range(200)), "chunk-a"),
        _candidate(" ".join(f"b{index}" for index in range(200)), "chunk-b"),
        _candidate("short", "chunk-c"),
    ]

    class ScoreByPrefix:
        def predict(self, pairs):
            # Longest clipped document wins; clipping shrinks every long pair
            # to the same length, so the score must come from content, not size.
            return [10.0 if document.startswith("a") else 1.0 for _, document in pairs]

    reranked = CrossEncoderReranker(model=ScoreByPrefix(), max_chars=512).rerank(
        "query", candidates, top_k=3
    )
    assert [hit.chunk_id for hit in reranked] == ["chunk-a", "chunk-b", "chunk-c"]
    assert [hit.rank for hit in reranked] == [1, 2, 3]


def test_empty_candidate_list_short_circuits_without_touching_the_model():
    model = RecordingCrossEncoder()
    assert CrossEncoderReranker(model=model, max_chars=8).rerank("query", [], top_k=3) == []
    assert model.pairs == []


# --- OnnxReranker (the opt-in int8 backend) ----------------------------------
#
# The 570 MB int8 graph is a local build product (`artifacts/` is gitignored),
# so these tests drive the loader through its injected session/tokenizer — the
# same seam production uses. What the *artifact* scores is measured by
# `scripts/benchmark_reranker_backends.py`, not asserted here.


class _StubTokenizer:
    """Records the pairs it is handed and emits fixed-shape arrays."""

    def __init__(self):
        self.calls = []
        self.documents = []

    def __call__(self, queries, documents, **kwargs):
        import numpy as np

        self.calls.append({"queries": list(queries), "documents": list(documents), **kwargs})
        self.documents = list(documents)
        return {
            "input_ids": np.ones((len(documents), 4), dtype=np.int64),
            "attention_mask": np.ones((len(documents), 4), dtype=np.int64),
        }


class _StubSession:
    """One logit per pair, `-len(document)`, so ordering is predictable."""

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.feeds = []

    def run(self, output_names, feed):
        import numpy as np

        self.feeds.append(feed)
        return [np.array([[-float(len(d))] for d in self.tokenizer.documents], dtype=np.float32)]


def _artifact_dir(tmp_path, **manifest):
    directory = tmp_path / "onnx"
    directory.mkdir()
    (directory / "reranker_int8.onnx").write_bytes(b"not-a-real-graph")
    (directory / "manifest.json").write_text(
        json.dumps({"model_id": "stub-model", "max_tokens": 512, **manifest}), encoding="utf-8"
    )
    return directory


def _stub_reranker(tmp_path, **kwargs):
    tokenizer = _StubTokenizer()
    reranker = OnnxReranker(
        model_dir=_artifact_dir(tmp_path, **kwargs.pop("manifest", {})),
        tokenizer=tokenizer,
        session=_StubSession(tokenizer),
        **kwargs,
    )
    return reranker, tokenizer


def test_onnx_reranker_requires_a_local_artifact(tmp_path):
    with pytest.raises(FileNotFoundError, match="export_reranker_onnx"):
        OnnxReranker(model_dir=tmp_path / "missing")


def test_onnx_reranker_reports_a_missing_graph_before_building_a_session(tmp_path):
    directory = tmp_path / "onnx"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({"model_id": "stub"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="ONNX graph missing"):
        OnnxReranker(model_dir=directory, tokenizer=_StubTokenizer())


def test_onnx_reranker_scores_every_candidate_and_ranks_by_score(tmp_path):
    reranker, tokenizer = _stub_reranker(tmp_path, max_chars=0)
    candidates = [_candidate("short", "c-short"), _candidate("much longer document", "c-long")]
    ranked = reranker.rerank("query", candidates, top_k=2)

    # Sigmoid of -len(document): the shorter document scores higher.
    assert [hit.chunk_id for hit in ranked] == ["c-short", "c-long"]
    assert ranked[0].score > ranked[1].score
    assert 0.0 < ranked[1].score < ranked[0].score < 1.0
    assert [hit.rank for hit in ranked] == [1, 2]
    assert tokenizer.documents == ["short", "much longer document"]


def test_onnx_reranker_clips_the_document_and_forwards_the_token_budget(tmp_path):
    reranker, tokenizer = _stub_reranker(tmp_path, max_chars=16, manifest={"max_tokens": 128})
    assert reranker.max_tokens == 128
    reranker.rerank("我的查询", [_candidate("z" * 500)], top_k=1)

    call = tokenizer.calls[-1]
    assert call["queries"] == ["我的查询"]  # the query is never clipped
    assert call["documents"] == ["z" * 16]
    assert call["truncation"] is True
    assert call["max_length"] == 128


def test_onnx_reranker_defaults_the_token_budget_when_the_manifest_omits_it(tmp_path):
    reranker, _ = _stub_reranker(tmp_path)
    assert reranker.max_tokens == 512


def test_onnx_reranker_empty_candidates_short_circuit(tmp_path):
    reranker, tokenizer = _stub_reranker(tmp_path)
    assert reranker.rerank("query", [], top_k=3) == []
    assert tokenizer.calls == []


def test_create_reranker_dispatches_both_onnx_spellings(monkeypatch):
    sentinel = object()
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr("rag.reranker.OnnxReranker", fake)
    assert create_reranker("onnx_int8", model_dir="X", max_chars=7) is sentinel
    assert seen == {"model_name": RERANKER_MODEL, "model_dir": "X", "max_chars": 7}
    assert create_reranker("ONNX") is sentinel  # alias, case-insensitive


def test_create_reranker_refuses_onnx_when_the_artifact_is_absent(tmp_path):
    with pytest.raises(FileNotFoundError, match="export_reranker_onnx"):
        create_reranker("onnx_int8", model_dir=tmp_path / "missing")
