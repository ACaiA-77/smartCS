from __future__ import annotations

from pathlib import Path


def test_api_startup_does_not_seed_demo_knowledge_documents():
    source = Path("api/main.py").read_text(encoding="utf-8")

    assert "long_term_memory.add_document(" not in source
