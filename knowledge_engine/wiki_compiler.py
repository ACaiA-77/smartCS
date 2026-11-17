"""Incremental Obsidian wiki compiler and health linter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ontology import ALLOWED_ENTITY_TYPES, OntologyStore
from .vault import ObsidianVault, slugify

_COMPILER_PAGE_NAMES = frozenset({"index.md", "log.md"})
_SKIP_LOG_SOURCES = frozenset({"wiki/index.md", "wiki/log.md"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WikiCompiler:
    """Compile source documents into wiki pages, an index and a log.

    The compiler only writes through :class:`ObsidianVault` zone APIs, so raw
    snapshots stay immutable and the external ``notes/`` zone is never edited.
    """

    def __init__(
        self, vault: ObsidianVault, ontology: OntologyStore | None = None
    ) -> None:
        self.vault = vault
        self.ontology = ontology

    # --------------------------------------------------------------- compile
    def ingest_document(
        self,
        source_uri: str,
        title: str,
        raw_content: str,
        summary_markdown: str,
        slug: str | None = None,
        related_entities: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Archive one source, compile its wiki page and refresh index/log."""

        page_slug = slugify(slug or title)
        raw_path = self.vault.archive_raw(
            page_slug,
            title,
            raw_content,
            source_uri=source_uri,
            metadata={"kind": "document", "slug": page_slug},
        )
        raw_rel = raw_path.relative_to(self.vault.root).as_posix()
        content = self._compiled_content(summary_markdown, raw_rel, source_uri)
        wiki_path = self.vault.write_wiki_page(page_slug, title, content)

        note_entity: dict[str, Any] | None = None
        linked_entities: list[dict[str, Any]] = []
        if self.ontology is not None:
            wiki_rel = wiki_path.relative_to(self.vault.root).as_posix()
            note_entity = self.ontology.upsert_entity(
                "NotePage",
                title,
                canonical_key=f"notepage:{page_slug}",
                attrs={"slug": page_slug, "path": wiki_rel, "source_uri": source_uri},
            )
            for item in related_entities or []:
                linked_entities.append(
                    self._upsert_related(note_entity["id"], item, source_uri)
                )

        index_path = self.rebuild_index()
        log_path = self.vault.append_wiki_log(
            f"- {_utc_now()} ingested [[{page_slug}]] from {source_uri}"
        )
        return {
            "slug": page_slug,
            "title": title,
            "raw_path": raw_path,
            "wiki_path": wiki_path,
            "index_path": index_path,
            "log_path": log_path,
            "note_entity_id": note_entity["id"] if note_entity else None,
            "linked_entities": linked_entities,
        }

    @staticmethod
    def _compiled_content(
        summary_markdown: str, raw_rel: str, source_uri: str
    ) -> str:
        summary = str(summary_markdown).replace("\r\n", "\n").strip()
        return (
            f"{summary}\n\n"
            "## Source\n\n"
            f"- Raw snapshot: `{raw_rel}`\n"
            f"- Wikilink: [[{raw_rel}]]\n"
            f"- Source URI: {source_uri}\n"
        )

    def _upsert_related(
        self, note_entity_id: str, item: Mapping[str, Any], source_uri: str
    ) -> dict[str, Any]:
        if self.ontology is None:
            raise ValueError("related_entities requires an ontology store")
        entity_type = str(item.get("entity_type") or "").strip()
        if entity_type not in ALLOWED_ENTITY_TYPES:
            raise ValueError(f"unsupported related entity_type: {entity_type!r}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("related entity name must not be empty")
        entity = self.ontology.upsert_entity(
            entity_type,
            name,
            canonical_key=item.get("canonical_key"),
            attrs=item.get("attrs"),
        )
        default_alias_source = str(item.get("alias_source") or "ingest")
        for alias in item.get("aliases") or []:
            if isinstance(alias, Mapping):
                self.ontology.link_alias(
                    str(alias.get("alias_key") or ""),
                    entity["id"],
                    str(alias.get("source") or default_alias_source),
                    float(alias.get("confidence", 1.0)),
                )
            else:
                self.ontology.link_alias(
                    str(alias), entity["id"], default_alias_source
                )
        relation = str(item.get("relation") or "mentions").strip() or "mentions"
        self.ontology.add_relation(
            note_entity_id, relation, entity["id"], evidence_uri=source_uri
        )
        return entity

    # ----------------------------------------------------------------- index
    def _wiki_page_rel_paths(self) -> list[str]:
        rels: list[str] = []
        for path in self.vault.list_notes("wiki"):
            rel = path.relative_to(self.vault.root).as_posix()
            if rel.rsplit("/", 1)[-1] in _COMPILER_PAGE_NAMES:
                continue
            rels.append(rel)
        return sorted(rels, key=str.casefold)

    def _read_notes(self, zones: tuple[str, ...]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        for zone in zones:
            for path in self.vault.list_notes(zone):
                rel = path.relative_to(self.vault.root).as_posix()
                notes.append(self.vault.read_note(rel))
        return sorted(notes, key=lambda note: str(note["path"]).casefold())

    def rebuild_index(self) -> Path:
        """Regenerate ``wiki/index.md`` as a deterministic, sorted page list."""

        pages = [self.vault.read_note(rel) for rel in self._wiki_page_rel_paths()]
        pages.sort(key=lambda note: str(note["path"]).casefold())
        lines = [
            "# Wiki Index",
            "",
            "> Auto-generated by WikiCompiler. Do not edit manually.",
            "",
            f"## Pages ({len(pages)})",
            "",
        ]
        if pages:
            for note in pages:
                target = Path(str(note["path"])).stem
                lines.append(f"- [[{target}|{note['title']}]]")
        else:
            lines.append("_No compiled pages yet._")
        body = "\n".join(lines) + "\n"
        return self.vault.write_wiki_page(
            "index", "Wiki Index", body, tags=["index"]
        )

    # ------------------------------------------------------------------ lint
    def _wiki_link_keys(self) -> dict[str, set[str]]:
        keys: dict[str, set[str]] = {}
        for rel in self._wiki_page_rel_paths():
            stem = Path(rel).stem.casefold()
            rel_without_ext = rel[:-3] if rel.casefold().endswith(".md") else rel
            keys.setdefault(stem, set()).add(rel)
            keys.setdefault(rel_without_ext.casefold(), set()).add(rel)
        return keys

    def lint_wiki(self) -> dict[str, Any]:
        """Report broken links, orphan wiki pages and pages missing from the index."""

        wiki_notes = self._read_notes(("wiki",))
        broken: set[tuple[str, str]] = set()
        for note in wiki_notes:
            for link in note["links"]:
                target = str(link["target"])
                if not self.vault.link_exists(target):
                    broken.add((str(note["path"]), target))

        sources = [
            note
            for note in self._read_notes(("wiki", "notes", "daily"))
            if str(note["path"]) not in _SKIP_LOG_SOURCES
        ]
        link_keys = self._wiki_link_keys()
        incoming: set[str] = set()
        for note in sources:
            for link in note["links"]:
                target = str(link["target"]).casefold()
                incoming.update(link_keys.get(target, set()))

        wiki_pages = self._wiki_page_rel_paths()
        orphans = [rel for rel in wiki_pages if rel not in incoming]

        index_path = self.vault.root / "wiki" / "index.md"
        if index_path.is_file():
            index_note = self.vault.read_note("wiki/index.md")
            indexed = {
                str(link["target"]).casefold() for link in index_note["links"]
            }
            for link in index_note["links"]:
                indexed.add(Path(str(link["target"])).stem.casefold())
            unindexed = [
                rel
                for rel in wiki_pages
                if Path(rel).stem.casefold() not in indexed
                and (rel[:-3] if rel.endswith(".md") else rel).casefold() not in indexed
            ]
        else:
            unindexed = list(wiki_pages)

        return {
            "broken_links": [
                {"path": path, "target": target}
                for path, target in sorted(broken)
            ],
            "orphan_pages": sorted(orphans, key=str.casefold),
            "unindexed_pages": sorted(unindexed, key=str.casefold),
        }
