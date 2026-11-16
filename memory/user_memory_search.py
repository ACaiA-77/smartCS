"""Lexical matching for user-memory retrieval.

Tokenization, query-term bounds and the overlap score lived at the bottom of
``memory/user_memory.py`` while being used by both the service and the
in-memory repository. They are here so the ranking rules can be read — and
tested — on their own; the SQL prefilter and the storage classes stay where
they are.

Semantics are preserved exactly: identifier characters survive, CJK text is
matched by characters and bigrams, stop words are dropped, at most 24 query
terms are used, and metadata-only fields never contribute to the body text.
"""

from __future__ import annotations

import re
from typing import Any

#: Fields that describe where a memory came from, not what it says.
SEMANTIC_TEXT_IGNORED_KEYS = frozenset({
    "source", "source_readonly_hint", "not_authoritative", "requires_live_tool_lookup",
    "source_event_id", "session_id", "confidence", "importance", "created_at",
    "derived_abstraction",
})

_MAX_SEARCH_TERMS = 24

STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with", "is", "it", "my", "i",
    "me", "this", "that", "what", "when", "where", "how", "do", "does", "can", "you", "please",
    "的", "了", "吗", "呢", "我", "你", "他", "她", "它", "是", "在", "请", "一下", "什么",
})


def semantic_text(value: Any) -> str:
    """Flatten a memory value into searchable text, dropping provenance keys."""
    if isinstance(value, dict):
        return " ".join(
            semantic_text(item)
            for key, item in value.items()
            if key not in SEMANTIC_TEXT_IGNORED_KEYS
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(semantic_text(item) for item in value)
    return str(value) if value is not None else ""


def search_terms(terms: list[str]) -> list[str]:
    """Normalize query terms: lowercase, identifier-shaped, unique, bounded."""
    normalized: list[str] = []
    for raw in terms:
        term = str(raw).lower().strip()
        if not term or len(term) > 64 or not re.fullmatch(r"[a-z0-9_一-鿿]+", term):
            continue
        if term not in normalized:
            normalized.append(term)
        if len(normalized) >= _MAX_SEARCH_TERMS:
            break
    return normalized


def tokens(text: str) -> list[str]:
    """Words, CJK characters and CJK bigrams, minus stop words and singles."""
    normalized = (text or "").lower()
    words = re.findall(r"[a-z0-9_]+", normalized)
    cjk_chars = re.findall(r"[一-鿿]", normalized)
    cjk_bigrams = [normalized[index:index + 2] for index in range(max(len(normalized) - 1, 0))]
    cjk_bigrams = [item for item in cjk_bigrams if re.search(r"[一-鿿]", item)]
    return [item for item in words + cjk_chars + cjk_bigrams if item not in STOP_WORDS and len(item) > 1 or item in {"a", "i"}]


def score(query_terms: set[str], text: str) -> float:
    """Share of the query terms that appear in ``text``; 0.0 without overlap."""
    body_terms = set(tokens(text))
    if not query_terms or not body_terms:
        return 0.0
    return len(query_terms & body_terms) / len(query_terms)
