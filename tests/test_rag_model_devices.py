"""Device policy tests: no torch kernels, downloads, models, or LLM calls."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from memory import long_term
from rag import embeddings
from rag.model_devices import EMBEDDING_DEVICE_ENV, RERANK_DEVICE_ENV, RERANK_DTYPE_ENV
from rag.models import RetrievalHit
from rag.reranker import CrossEncoderReranker, create_reranker


def parameter(device="cpu", dtype="torch.float32", count=10, floating=True):
    return SimpleNamespace(device=device, dtype=dtype, numel=lambda: count,
                           is_floating_point=lambda: floating)


def predictor(*parameters):
    # Old CrossEncoder versions expose the underlying AutoModel here.
    return SimpleNamespace(model=SimpleNamespace(parameters=lambda: iter(parameters)), predict=Mock())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in (RERANK_DEVICE_ENV, RERANK_DTYPE_ENV, EMBEDDING_DEVICE_ENV):
        monkeypatch.delenv(name, raising=False)
    # Any accidental real import/loading is a failure, never an online test.
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setitem(sys.modules, "torch", None)


@pytest.fixture
def loaders(monkeypatch):
    torch = SimpleNamespace(
        float32="torch.float32", float16="torch.float16",
        cuda=SimpleNamespace(is_available=Mock(return_value=True), device_count=Mock(return_value=1)),
        backends=SimpleNamespace(cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=True)),
                                 cudnn=SimpleNamespace(allow_tf32=True)),
        set_float32_matmul_precision=Mock(),
    )
    cross = Mock(return_value=predictor(parameter()))
    model = SimpleNamespace(parameters=lambda: iter([parameter()]),
                            get_sentence_embedding_dimension=lambda: 4,
                            encode=Mock(return_value=np.array([[1., 0., 0., 0.]])))
    sentence = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        SimpleNamespace(CrossEncoder=cross, SentenceTransformer=sentence))
    return torch, cross, sentence


@pytest.mark.parametrize("setting", [None, "auto"])
def test_default_injected_legacy_model_never_imports_torch_or_st(monkeypatch, caplog, setting):
    if setting is not None:
        monkeypatch.setenv(RERANK_DEVICE_ENV, setting)
    model = SimpleNamespace(predict=Mock(return_value=[1.]))
    with caplog.at_level(logging.WARNING):
        reranker = CrossEncoderReranker(model=model)
    hit = RetrievalHit("a", "apple_support", 1, .1, "a.md", "short")
    assert reranker.rerank("query", [hit])[0].rerank_score == 1.
    model.predict.assert_called_once_with([("query", "short")])
    assert "path=injected" in caplog.text
    assert "verification=skipped" in caplog.text
    assert "verification=passed" not in caplog.text
    assert "cuda" not in caplog.text


@pytest.mark.parametrize("setting", [None, "auto"])
def test_real_loader_preserves_auto_device_and_default_activation(monkeypatch, loaders, setting):
    if setting is not None:
        monkeypatch.setenv(RERANK_DEVICE_ENV, setting)
    torch, cross, _ = loaders
    CrossEncoderReranker(model_name="test-model")
    cross.assert_called_once_with("test-model", model_kwargs={"torch_dtype": torch.float32})
    assert "device" not in cross.call_args.kwargs
    assert not ({"activation_fn", "default_activation_function", "local_files_only"} & cross.call_args.kwargs.keys())
    assert torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.backends.cudnn.allow_tf32 is False
    torch.set_float32_matmul_precision.assert_called_once_with("highest")


@pytest.mark.parametrize("device", [None, "cuda:0"])
def test_fp32_cuda_preserves_legacy_torch_without_precision_api(monkeypatch, loaders, device):
    if device is not None:
        monkeypatch.setenv(RERANK_DEVICE_ENV, device)
    torch, cross, _ = loaders
    del torch.set_float32_matmul_precision
    cross.return_value = predictor(parameter("cuda:0"))
    CrossEncoderReranker(model_name="test-model")
    cross.assert_called_once()
    assert torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.backends.cudnn.allow_tf32 is False


def test_explicit_cpu_preserves_prediction_budget_order_and_safe_startup_log(monkeypatch, loaders, caplog):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cpu")
    monkeypatch.setenv("SMARTCS_RERANK_MAX_CHARS", "0")
    torch, cross, _ = loaders
    cross.return_value.predict.return_value = [.5, .5, .9]
    candidates = [RetrievalHit(i, "apple_support", 1, .1, "a.md", "x" * 1000) for i in ("b", "a", "c")]
    with caplog.at_level(logging.WARNING):
        reranker = create_reranker("cross_encoder", model_name="test-model")
        ranked = reranker.rerank("private-query", candidates, top_k=3)
    cross.assert_called_once_with("test-model", device="cpu", model_kwargs={"torch_dtype": torch.float32})
    cross.return_value.predict.assert_called_once_with([("private-query", "x" * 1000)] * 3)
    assert [hit.chunk_id for hit in ranked] == ["c", "a", "b"]
    assert [hit.rank for hit in ranked] == [1, 2, 3]
    assert [hit.score for hit in ranked] == [.9, .5, .5]
    assert "model=test-model device=cpu dtype=torch.float32 parameter_count=10" in caplog.text
    assert "path=loaded" in caplog.text and "pid=" in caplog.text
    assert [(record.name, record.levelno) for record in caplog.records] == [("rag.model_devices", logging.WARNING)]
    assert "private-query" not in caplog.text
    torch.set_float32_matmul_precision.assert_not_called()


@pytest.mark.parametrize("device", ["cuda", "cuda:0"])
@pytest.mark.parametrize("dtype", ["fp32", "fp16"])
def test_cuda_direct_load_precision_and_actual_parameter_log(monkeypatch, loaders, caplog, device, dtype):
    torch, cross, _ = loaders
    monkeypatch.setenv(RERANK_DEVICE_ENV, device)
    monkeypatch.setenv(RERANK_DTYPE_ENV, dtype)
    actual_dtype = torch.float32 if dtype == "fp32" else torch.float16
    # ST 5/6 exposes parameters directly; wrapper.device is deliberately wrong.
    cross.return_value = SimpleNamespace(parameters=lambda: iter([parameter("cuda:0", actual_dtype)]), device="cpu")
    with caplog.at_level(logging.WARNING):
        CrossEncoderReranker(model_name="test-model")
    cross.assert_called_once_with("test-model", device="cuda:0",
                                  model_kwargs={"torch_dtype": actual_dtype, "device_map": {"": "cuda:0"}})
    assert f"device=cuda:0 dtype={actual_dtype} parameter_count=10 verification=passed" in caplog.text
    assert "path=loaded" in caplog.text
    assert [(record.name, record.levelno) for record in caplog.records] == [("rag.model_devices", logging.WARNING)]
    assert torch.backends.cuda.matmul.allow_tf32 is (dtype == "fp16")
    assert torch.backends.cudnn.allow_tf32 is (dtype == "fp16")


@pytest.mark.parametrize("device", ["cuda", "cuda:0"])
@pytest.mark.parametrize("available,count", [(False, 0), (True, 0)])
def test_cuda_unavailable_fails_before_loading(monkeypatch, loaders, device, available, count):
    torch, cross, _ = loaders
    monkeypatch.setenv(RERANK_DEVICE_ENV, device)
    torch.cuda.is_available.return_value = available
    torch.cuda.device_count.return_value = count
    with pytest.raises(RuntimeError, match="CUDA.*unavailable"):
        CrossEncoderReranker()
    cross.assert_not_called()


@pytest.mark.parametrize("env,value", [(RERANK_DEVICE_ENV, "gpu"), (RERANK_DEVICE_ENV, "cuda:1"),
                                       (RERANK_DEVICE_ENV, ""), (RERANK_DTYPE_ENV, "bf16"),
                                       (RERANK_DTYPE_ENV, "")])
def test_invalid_rerank_policy_is_rejected_without_imports(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match=env):
        CrossEncoderReranker()


def test_cpu_fp16_rejected_before_loading(monkeypatch, loaders):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cpu")
    monkeypatch.setenv(RERANK_DTYPE_ENV, "fp16")
    with pytest.raises(ValueError, match="CPU fp16"):
        CrossEncoderReranker()
    loaders[1].assert_not_called()


def test_auto_resolved_cpu_fp16_is_rejected(monkeypatch, loaders):
    monkeypatch.setenv(RERANK_DTYPE_ENV, "fp16")
    loaders[1].return_value = predictor(parameter(dtype="torch.float16"))
    with pytest.raises(ValueError, match="CPU fp16"):
        CrossEncoderReranker()


@pytest.mark.parametrize("parameters", [[], [parameter("cpu", "torch.float16")],
    [parameter("cuda:1", "torch.float16")], [parameter("meta", "torch.float16")],
    [parameter("cuda:0", "torch.float32")],
    [parameter("cuda:0", "torch.float16"), parameter("cuda:0", "torch.float32")],
    [parameter("cuda:0", "torch.float16"), parameter("cpu", "torch.int64", floating=False)]])
def test_actual_cuda_device_or_dtype_mismatch_rejected(monkeypatch, loaders, caplog, parameters):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cuda")
    monkeypatch.setenv(RERANK_DTYPE_ENV, "fp16")
    loaders[1].return_value = predictor(*parameters)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="parameters|mismatch"):
        CrossEncoderReranker()
    assert "verification=passed" not in caplog.text


def test_default_fp32_validates_actual_dtype(loaders):
    loaders[1].return_value = predictor(parameter(dtype="torch.float16"))
    with pytest.raises(RuntimeError, match="dtype mismatch"):
        CrossEncoderReranker()


def test_injected_parameterless_cuda_model_logs_only_unverified_path(monkeypatch, loaders, caplog):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cuda")
    model = SimpleNamespace(predict=Mock())
    with caplog.at_level(logging.WARNING):
        CrossEncoderReranker(model=model)
    assert "path=injected" in caplog.text
    assert "parameters=unavailable verification=skipped" in caplog.text
    assert [(record.name, record.levelno) for record in caplog.records] == [("rag.model_devices", logging.WARNING)]
    assert "device=cuda" not in caplog.text
    assert "verification=passed" not in caplog.text
    loaders[1].assert_not_called()


def test_injected_cpu_legacy_model_remains_usable_without_torch(monkeypatch):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cpu")
    model = SimpleNamespace(predict=Mock())
    assert CrossEncoderReranker(model=model)._model is model


def test_cuda_fp32_rejects_actual_half_parameters(monkeypatch, loaders):
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cuda")
    loaders[1].return_value = predictor(parameter("cuda:0", "torch.float16"))
    with pytest.raises(RuntimeError, match="dtype mismatch"):
        CrossEncoderReranker()


def test_real_parameterless_model_is_not_claimed_as_verified(loaders):
    loaders[1].return_value = SimpleNamespace(device="cuda:0", dtype="torch.float32")
    with pytest.raises(RuntimeError, match="no inspectable parameters"):
        CrossEncoderReranker()


def test_legacy_st_constructor_argument_compatibility(monkeypatch, loaders):
    seen = {}

    def legacy(name, *, device=None, automodel_args=None):
        seen.update(name=name, device=device, automodel_args=automodel_args)
        return predictor(parameter())

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=legacy))
    monkeypatch.setenv(RERANK_DEVICE_ENV, "cpu")
    CrossEncoderReranker(model_name="test-model")
    assert seen == {"name": "test-model", "device": "cpu", "automodel_args": {"torch_dtype": loaders[0].float32}}


@pytest.mark.parametrize("backend", [embeddings.SentenceTransformerEmbeddingBackend, long_term.SentenceTransformerEmbeddingBackend])
@pytest.mark.parametrize("device", [None, "auto", "cpu", "cuda", "cuda:0"])
def test_both_embedding_constructors_honor_device(monkeypatch, loaders, backend, device):
    if device is not None:
        monkeypatch.setenv(EMBEDDING_DEVICE_ENV, device)
    _, _, sentence = loaders
    expected = "cuda:0" if device in {"cuda", "cuda:0"} else "cpu"
    sentence.return_value.parameters = lambda: iter([parameter(expected)])
    result = backend("embedding-model")
    kwargs = {} if device in {None, "auto"} else {"device": expected}
    sentence.assert_called_once_with("embedding-model", **kwargs)
    assert result.dimension == 4
    assert np.array_equal(result.embed_batch(["text"])[0], np.array([1., 0., 0., 0.]))
    sentence.return_value.encode.assert_called_once_with(["text"], normalize_embeddings=True)


@pytest.mark.parametrize("backend", [embeddings.SentenceTransformerEmbeddingBackend, long_term.SentenceTransformerEmbeddingBackend])
@pytest.mark.parametrize("device", ["gpu", "cuda:1", ""])
def test_invalid_embedding_device_rejected(monkeypatch, backend, device):
    monkeypatch.setenv(EMBEDDING_DEVICE_ENV, device)
    with pytest.raises(ValueError, match=EMBEDDING_DEVICE_ENV):
        backend("embedding-model")


@pytest.mark.parametrize("backend", [embeddings.SentenceTransformerEmbeddingBackend, long_term.SentenceTransformerEmbeddingBackend])
def test_embedding_cuda_unavailable_fails_before_loading(monkeypatch, loaders, backend):
    monkeypatch.setenv(EMBEDDING_DEVICE_ENV, "cuda")
    loaders[0].cuda.is_available.return_value = False
    with pytest.raises(RuntimeError, match="CUDA.*unavailable"):
        backend("embedding-model")
    loaders[2].assert_not_called()


@pytest.mark.parametrize("backend", [embeddings.SentenceTransformerEmbeddingBackend, long_term.SentenceTransformerEmbeddingBackend])
def test_embedding_no_cpu_fallback_on_actual_device_mismatch(monkeypatch, loaders, backend):
    monkeypatch.setenv(EMBEDDING_DEVICE_ENV, "cuda")
    with pytest.raises(RuntimeError, match="device mismatch"):
        backend("embedding-model")


@pytest.mark.parametrize("device", ["cpu", "cuda", "invalid"])
def test_memory_auto_backend_does_not_swallow_explicit_policy_errors(monkeypatch, device):
    monkeypatch.setenv("EMBEDDING_BACKEND", "auto")
    monkeypatch.setenv(EMBEDDING_DEVICE_ENV, device)
    with pytest.raises((ImportError, RuntimeError, ValueError)):
        long_term.create_embedding_backend()


def test_memory_auto_unset_keeps_original_hash_fallback(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "auto")
    assert isinstance(long_term.create_embedding_backend(4), long_term.HashEmbeddingBackend)


def test_hash_and_openai_backends_ignore_unrelated_local_model_device(monkeypatch):
    monkeypatch.setenv(EMBEDDING_DEVICE_ENV, "invalid")
    monkeypatch.setenv(RERANK_DEVICE_ENV, "invalid")
    assert embeddings.create_embedding_backend("hash", dimension=4).dimension == 4
    assert create_reranker("fake").backend_name == "fake"
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    assert isinstance(long_term.create_embedding_backend(4), long_term.HashEmbeddingBackend)
    remote = Mock(return_value=SimpleNamespace(dimension=4))
    monkeypatch.setattr(long_term, "OpenAIEmbeddingBackend", remote)
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    assert long_term.create_embedding_backend(4).dimension == 4
    remote.assert_called_once()
