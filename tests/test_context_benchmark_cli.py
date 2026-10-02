"""Safety guards for the real-database context benchmark command."""

from pathlib import Path

import pytest

from scripts import evaluate_context_engineering as benchmark


def test_context_benchmark_requires_explicit_database_opt_in(monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluate_context_engineering", "--output-root", "unused-output"])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert not Path("unused-output").exists()


@pytest.mark.asyncio
async def test_context_benchmark_rejects_existing_evidence_before_database_access(tmp_path, monkeypatch):
    output = tmp_path / "evidence"
    output.mkdir()
    original = output / "metrics.json"
    original.write_bytes(b"historical benchmark evidence")
    monkeypatch.setattr(benchmark, "load_dotenv", lambda: pytest.fail("configuration loaded before safety guard"))
    with pytest.raises(FileExistsError, match="overwrite"):
        await benchmark.evaluate(turns=50, output_root=output)
    assert original.read_bytes() == b"historical benchmark evidence"


@pytest.mark.asyncio
async def test_context_benchmark_rejects_unsupported_turn_counts_before_database_access(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "load_dotenv", lambda: pytest.fail("configuration loaded before bounds check"))
    with pytest.raises(ValueError, match="50 or 100"):
        await benchmark.evaluate(turns=0, output_root=tmp_path / "output")


def test_context_benchmark_percentile_and_lossless_value_traversal():
    assert benchmark._percentile([5.0, 1.0, 2.0], .95) == 2.0
    assert list(benchmark._all_values({"pending_action": {"order_id": "ORD-1", "amount": 598.00}})) == ["ORD-1", 598.00]
