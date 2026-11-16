"""Small device-policy helpers for the local RAG models (no eager torch import)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

RERANK_DEVICE_ENV = "SMARTCS_RERANK_DEVICE"
RERANK_DTYPE_ENV = "SMARTCS_RERANK_DTYPE"
EMBEDDING_DEVICE_ENV = "SMARTCS_EMBEDDING_DEVICE"


def device_from_env(name: str) -> str | None:
    """None delegates auto placement to sentence-transformers, as before."""
    value = os.getenv(name, "auto").strip().lower()
    if value not in {"auto", "cpu", "cuda", "cuda:0"}:
        raise ValueError(f"{name} must be auto|cpu|cuda|cuda:0")
    if value == "auto":
        return None
    return "cuda:0" if value == "cuda" else value


def rerank_dtype_from_env(device: str | None) -> str:
    value = os.getenv(RERANK_DTYPE_ENV, "fp32").strip().lower()
    if value not in {"fp32", "fp16"}:
        raise ValueError(f"{RERANK_DTYPE_ENV} must be fp32|fp16")
    if device == "cpu" and value == "fp16":
        raise ValueError("CPU fp16 reranking is not supported")
    return value


def require_device_available(device: str | None) -> None:
    if device != "cuda:0":
        return
    import torch

    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("explicit CUDA device cuda:0 is unavailable; CPU fallback is forbidden")


def disable_tf32(torch) -> None:
    """FP32 CUDA must not silently use the lower precision TF32 kernels."""
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    # Older supported torch releases have the backend flags but not this API.
    set_precision = getattr(torch, "set_float32_matmul_precision", None)
    if callable(set_precision):
        set_precision("highest")


def verify_and_log_model(
    model,
    model_name: str,
    device: str | None,
    dtype: str | None = None,
    *,
    injected: bool = False,
) -> None:
    """Inspect weights, not the requested settings or wrapper's device property.

    CrossEncoder 3/4 exposes weights under `.model`; newer releases and
    SentenceTransformer expose `.parameters()` directly. Parameter-less injected
    test doubles remain usable, but never produce a verified GPU startup log.
    """
    owner = model if callable(getattr(model, "parameters", None)) else getattr(model, "model", None)
    parameters = getattr(owner, "parameters", None)
    if not callable(parameters):
        if not injected:
            raise RuntimeError("loaded model has no inspectable parameters")
        logger.warning(
            "RAG model startup pid=%s path=injected model=%s parameters=unavailable verification=skipped",
            os.getpid(), model_name,
        )
        return

    devices, dtypes = set(), set()
    expected_dtype = {"fp32": "torch.float32", "fp16": "torch.float16"}.get(dtype)
    count = 0
    for parameter in parameters():
        actual_device = str(parameter.device)
        devices.add(actual_device)
        count += parameter.numel()
        if actual_device == "meta" or (device is not None and actual_device != device):
            raise RuntimeError(
                f"model parameter device mismatch: expected {device or 'materialized'}, got {actual_device}"
            )
        if parameter.is_floating_point():
            actual_dtype = str(parameter.dtype)
            dtypes.add(actual_dtype)
            if dtype == "fp16" and actual_device == "cpu":
                raise ValueError("CPU fp16 reranking is not supported")
            if expected_dtype is not None and actual_dtype != expected_dtype:
                raise RuntimeError(
                    f"model parameter dtype mismatch: expected {expected_dtype}, got {actual_dtype}"
                )
    if not count or not dtypes:
        raise RuntimeError("loaded model has no floating parameters to verify")
    logger.warning(
        "RAG model startup pid=%s path=%s model=%s device=%s dtype=%s parameter_count=%s verification=passed",
        os.getpid(), "injected" if injected else "loaded", model_name,
        ",".join(sorted(devices)), ",".join(sorted(dtypes)), count,
    )
