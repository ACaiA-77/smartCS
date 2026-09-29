import json
from pathlib import Path

from scripts import report_rag_round3


def test_default_index_resolves_frozen_qrels_without_changing_history(tmp_path):
    history = [Path("artifacts/rag_round3") / name for name in (
        "query_overlap_report.json", "failure_analysis.md",
    )]
    before = {path: path.read_bytes() for path in history}
    benchmark = Path("benchmarks/rag")
    assert len((benchmark / "qrels.jsonl").read_text(encoding="utf-8").splitlines()) == 95

    report_rag_round3.write_reports(benchmark, tmp_path)

    report = json.loads((tmp_path / "query_overlap_report.json").read_text(encoding="utf-8"))
    assert report["query_count"] == len(report["rows"]) == 60
    assert all(row["qrel_sources"] for row in report["rows"])
    assert (tmp_path / "failure_analysis.md").is_file()
    assert {path: path.read_bytes() for path in history} == before


def test_cli_defaults_keep_output_separate_from_historical_v3(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["report_rag_round3"])
    monkeypatch.setattr(report_rag_round3, "write_reports", lambda *args: calls.append(args))

    assert report_rag_round3.main() == 0
    assert calls == [(
        Path("benchmarks/rag"), Path("artifacts/rag_round3_v4"),
        Path("artifacts/rag_round3/production_indexes"),
    )]
