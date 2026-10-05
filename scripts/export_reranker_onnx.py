"""Phase 12 §2: export the Cross-Encoder reranker to an int8 ONNX artifact.

This is the build half of the optional `onnx_int8` reranker backend. It turns
the same local `BAAI/bge-reranker-v2-m3` weights the `cross_encoder` backend
loads into a dynamically-quantized ONNX graph, so the two backends can be
compared on identical inputs (see `scripts/benchmark_reranker_backends.py`).

    python -m scripts.export_reranker_onnx                  # -> artifacts/reranker_onnx
    python -m scripts.export_reranker_onnx --skip-fp32      # reuse the fp32 graph

The output directory is gitignored (`/artifacts/`), so the artifact is a local
build product, never a committed blob. `manifest.json` records the model id,
the token limits and the file hashes the loader checks before trusting it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_OUTPUT = Path("artifacts/reranker_onnx")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pair_scorer_class():
    """Build the nn.Module wrapper at import time of the exporter, not the module.

    `torch.onnx.export` (dynamo path, torch >= 2.9) insists on a real
    `torch.nn.Module`, so this cannot be a plain callable. Importing torch at
    module scope would make the whole script unimportable without it, hence the
    factory.
    """
    import torch

    class PairScorer(torch.nn.Module):
        """Expose one HF sequence classifier as a 2-input graph (ids + mask)."""

        def __init__(self, model: Any):
            super().__init__()
            self.model = model

        def forward(self, input_ids, attention_mask):
            return self.model(input_ids=input_ids, attention_mask=attention_mask).logits

    return PairScorer


def resolve_max_tokens(model_id: str) -> int:
    """Token budget per pair, matching what sentence-transformers would use."""

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    configured = getattr(tokenizer, "model_max_length", None)
    if not isinstance(configured, int) or configured <= 0 or configured > 1_000_000:
        return 512
    return configured


def export_fp32(model_id: str, output_dir: Path, *, opset: int, exporter: str = "legacy") -> Path:
    import torch
    from transformers import AutoModelForSequenceClassification

    print(f"[1/2] loading {model_id} for export", file=sys.stderr)
    model = AutoModelForSequenceClassification.from_pretrained(model_id)
    model.eval()

    input_ids = torch.ones((2, 8), dtype=torch.long)
    attention_mask = torch.ones((2, 8), dtype=torch.long)

    target = output_dir / "reranker_fp32.onnx"
    print(f"[1/2] exporting opset={opset} exporter={exporter} -> {target}", file=sys.stderr)
    started = time.perf_counter()
    torch.onnx.export(
        _pair_scorer_class()(model),
        (input_ids, attention_mask),
        str(target),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "logits": {0: "batch"},
        },
        opset_version=opset,
        do_constant_folding=True,
        # The TorchScript exporter, not the torch.export/dynamo one. The dynamo
        # graph exports fine but bakes a shape that onnxruntime's quantizer
        # refuses to shape-infer ("dimension 0: (1024) vs (1)"), and the
        # quantizer has no opt-out. Measured, not assumed.
        dynamo=exporter == "dynamo",
    )
    print(f"[1/2] exported in {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return target


def quantize_int8(fp32_path: Path, output_dir: Path) -> Path:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    target = output_dir / "reranker_int8.onnx"
    print(f"[2/2] dynamic int8 quantization -> {target}", file=sys.stderr)
    started = time.perf_counter()
    quantize_dynamic(
        model_input=str(fp32_path),
        model_output=str(target),
        weight_type=QuantType.QInt8,
    )
    print(f"[2/2] quantized in {time.perf_counter() - started:.1f}s", file=sys.stderr)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--exporter", choices=("legacy", "dynamo"), default="legacy")
    parser.add_argument("--skip-fp32", action="store_true", help="reuse an existing reranker_fp32.onnx")
    parser.add_argument("--drop-fp32", action="store_true", help="delete the fp32 graph after quantizing")
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fp32_path = args.output_dir / "reranker_fp32.onnx"
    if not args.skip_fp32:
        fp32_path = export_fp32(args.model, args.output_dir, opset=args.opset, exporter=args.exporter)
    elif not fp32_path.is_file():
        raise SystemExit(f"--skip-fp32 given but {fp32_path} does not exist")

    int8_path = quantize_int8(fp32_path, args.output_dir)
    if args.drop_fp32:
        fp32_path.unlink()

    max_tokens = resolve_max_tokens(args.model)
    print(f"tokenizer max tokens per pair: {max_tokens}", file=sys.stderr)
    manifest = {
        "model_id": args.model,
        "opset": args.opset,
        "max_tokens": max_tokens,
        "activation": "sigmoid",
        "weight_type": "QInt8",
        "quantization": "dynamic",
        "file_sha256": {
            int8_path.name: _sha256(int8_path),
            **({fp32_path.name: _sha256(fp32_path)} if fp32_path.is_file() else {}),
        },
        "file_bytes": {
            int8_path.name: int8_path.stat().st_size,
            **({fp32_path.name: fp32_path.stat().st_size} if fp32_path.is_file() else {}),
        },
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps({"status": "ready", "output_dir": str(args.output_dir), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
