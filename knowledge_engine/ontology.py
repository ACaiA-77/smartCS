"""SQLite-backed lightweight ontology with cross-platform alias resolution."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWED_ENTITY_TYPES = frozenset(
    {"Person", "Project", "RepoIssue", "MailThread", "NotePage", "ActionItem"}
)
_DIRECTIONS = frozenset({"out", "in", "both"})
_CANONICAL_DROP_RE = re.compile(r"[^\w-]+", re.UNICODE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical_key TEXT NOT NULL UNIQUE,
    attrs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_key TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    src_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type TEXT NOT NULL,
    dst_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    evidence_uri TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(src_id, rel_type, dst_id)
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_canonical(value: str) -> str:
    normalized = str(value).strip().casefold()
    canonical = _CANONICAL_DROP_RE.sub("-", normalized)
    return canonical.strip("-._")


class OntologyStore:
    """Entity/alias/relation storage in one SQLite database.

    Use as a context manager to guarantee the connection is closed. Foreign
    keys and WAL are enabled on construction so cascades and concurrent reads
    behave predictably.
    """

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if str(path.parent) not in {"", "."}:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> OntologyStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        self._conn.close()

    @staticmethod
    def _entity_from_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            attrs = json.loads(row["attrs_json"])
        except (TypeError, json.JSONDecodeError):
            attrs = {}
        return {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "name": row["name"],
            "canonical_key": row["canonical_key"],
            "attrs": attrs if isinstance(attrs, dict) else {},
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _check_entity_type(entity_type: str) -> str:
        value = str(entity_type or "").strip()
        if value not in ALLOWED_ENTITY_TYPES:
            allowed = ", ".join(sorted(ALLOWED_ENTITY_TYPES))
            raise ValueError(f"unsupported entity_type {entity_type!r}; expected {allowed}")
        return value

    def upsert_entity(
        self,
        entity_type: str,
        name: str,
        canonical_key: str | None = None,
        attrs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert or update one entity, keyed by its unique canonical key."""

        kind = self._check_entity_type(entity_type)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("entity name must not be empty")
        if canonical_key is None:
            canonical_key = f"{kind.casefold()}:{_normalize_canonical(clean_name)}"
        key = str(canonical_key).strip()
        if not key:
            raise ValueError("canonical_key must not be empty")
        attrs_json = json.dumps(dict(attrs or {}), ensure_ascii=False, sort_keys=True)
        now = _utc_now()
        with self._conn:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE canonical_key = ?", (key,)
            ).fetchone()
            if row is None:
                entity_id = uuid.uuid4().hex
                self._conn.execute(
                    "INSERT INTO entities "
                    "(id, entity_type, name, canonical_key, attrs_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (entity_id, kind, clean_name, key, attrs_json, now),
                )
            else:
                entity_id = row["id"]
                self._conn.execute(
                    "UPDATE entities SET name = ?, attrs_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (clean_name, attrs_json, now, entity_id),
                )
        entity = self.get_entity(entity_id)
        assert entity is not None
        return entity

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Return one entity by id, or None when it does not exist."""

        row = self._conn.execute(
            "SELECT * FROM entities WHERE id = ?", (str(entity_id),)
        ).fetchone()
        return self._entity_from_row(row) if row is not None else None

    def link_alias(
        self,
        alias_key: str,
        entity_id: str,
        source: str,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Bind a platform-specific alias to an entity, replacing stale bindings."""

        alias = str(alias_key or "").strip()
        if not alias:
            raise ValueError("alias_key must not be empty")
        if self.get_entity(entity_id) is None:
            raise ValueError(f"unknown entity: {entity_id}")
        alias_source = str(source or "").strip() or "unknown"
        score = float(confidence)
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO entity_aliases "
                "(alias_key, entity_id, source, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(alias_key) DO UPDATE SET "
                "entity_id = excluded.entity_id, source = excluded.source, "
                "confidence = excluded.confidence",
                (alias, str(entity_id), alias_source, score, now),
            )
        row = self._conn.execute(
            "SELECT * FROM entity_aliases WHERE alias_key = ?", (alias,)
        ).fetchone()
        return {
            "alias_key": row["alias_key"],
            "entity_id": row["entity_id"],
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "created_at": row["created_at"],
        }

    def resolve_by_alias(self, alias_key: str) -> dict[str, Any] | None:
        """Resolve an alias to its entity, falling back to a case-insensitive match."""

        alias = str(alias_key or "").strip()
        if not alias:
            return None
        query = (
            "SELECT e.*, a.alias_key AS matched_alias "
            "FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
            "WHERE a.alias_key = ?"
        )
        row = self._conn.execute(query, (alias,)).fetchone()
        if row is None:
            row = self._conn.execute(
                query + " COLLATE NOCASE", (alias,)
            ).fetchone()
        if row is None:
            return None
        entity = self._entity_from_row(row)
        entity["matched_alias"] = row["matched_alias"]
        return entity

    def add_relation(
        self,
        src_id: str,
        rel_type: str,
        dst_id: str,
        evidence_uri: str | None = None,
    ) -> dict[str, Any]:
        """Insert one directed relation, idempotently keyed by the triple."""

        kind = str(rel_type or "").strip()
        if not kind:
            raise ValueError("rel_type must not be empty")
        if self.get_entity(src_id) is None:
            raise ValueError(f"unknown source entity: {src_id}")
        if self.get_entity(dst_id) is None:
            raise ValueError(f"unknown destination entity: {dst_id}")
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO relations "
                "(id, src_id, rel_type, dst_id, evidence_uri, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(src_id, rel_type, dst_id) DO UPDATE SET "
                "evidence_uri = excluded.evidence_uri, "
                "updated_at = excluded.updated_at",
                (uuid.uuid4().hex, str(src_id), kind, str(dst_id), evidence_uri, now),
            )
        row = self._conn.execute(
            "SELECT * FROM relations "
            "WHERE src_id = ? AND rel_type = ? AND dst_id = ?",
            (str(src_id), kind, str(dst_id)),
        ).fetchone()
        return {
            "id": row["id"],
            "src_id": row["src_id"],
            "rel_type": row["rel_type"],
            "dst_id": row["dst_id"],
            "evidence_uri": row["evidence_uri"],
            "updated_at": row["updated_at"],
        }

    def get_neighbors(
        self,
        entity_id: str,
        rel_type: str | None = None,
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        """Return neighboring entities for one node, with relation metadata."""

        if direction not in _DIRECTIONS:
            raise ValueError(f"unsupported direction: {direction!r}")
        entity_id = str(entity_id)
        kind = str(rel_type).strip() if rel_type is not None else None
        results: list[dict[str, Any]] = []

        def collect(join_column: str, match_column: str, label: str) -> None:
            sql = (
                "SELECT r.id AS relation_id, r.rel_type, r.evidence_uri, "
                f"r.updated_at AS relation_updated_at, e.* "
                "FROM relations r "
                f"JOIN entities e ON e.id = r.{join_column} "
                f"WHERE r.{match_column} = ?"
            )
            params: list[Any] = [entity_id]
            if kind:
                sql += " AND r.rel_type = ?"
                params.append(kind)
            for row in self._conn.execute(sql, params):
                results.append(
                    {
                        "relation_id": row["relation_id"],
                        "rel_type": row["rel_type"],
                        "direction": label,
                        "evidence_uri": row["evidence_uri"],
                        "relation_updated_at": row["relation_updated_at"],
                        "entity": self._entity_from_row(row),
                    }
                )

        if direction in {"out", "both"}:
            collect("dst_id", "src_id", "out")
        if direction in {"in", "both"}:
            collect("src_id", "dst_id", "in")
        results.sort(
            key=lambda item: (
                str(item["rel_type"]).casefold(),
                str(item["direction"]),
                str(item["entity"]["name"]).casefold(),
                str(item["entity"]["id"]),
            )
        )
        return results

    def query_entities(
        self,
        entity_type: str | None = None,
        text: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search entities by type and optional name/canonical/alias/attrs text."""

        if limit < 0:
            raise ValueError("limit must be non-negative")
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("e.entity_type = ?")
            params.append(self._check_entity_type(entity_type))
        needle = str(text or "").strip()
        if needle:
            pattern = f"%{needle}%"
            clauses.append(
                "(e.name LIKE ? COLLATE NOCASE "
                "OR e.canonical_key LIKE ? COLLATE NOCASE "
                "OR e.attrs_json LIKE ? COLLATE NOCASE "
                "OR EXISTS (SELECT 1 FROM entity_aliases a "
                "WHERE a.entity_id = e.id AND a.alias_key LIKE ? COLLATE NOCASE))"
            )
            params.extend([pattern, pattern, pattern, pattern])
        sql = "SELECT DISTINCT e.* FROM entities e"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.name COLLATE NOCASE, e.id LIMIT ?"
        params.append(int(limit))
        return [self._entity_from_row(row) for row in self._conn.execute(sql, params)]

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity; aliases and relations cascade automatically."""

        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM entities WHERE id = ?", (str(entity_id),)
            )
        return cursor.rowcount > 0
