"""Offline acceptance tests for the knowledge_engine package."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from knowledge_engine import (
    ObsidianVault,
    OntologyStore,
    VaultSecurityError,
    WikiCompiler,
)
from knowledge_engine.ontology import ALLOWED_ENTITY_TYPES
from knowledge_engine.vault import parse_wikilinks, slugify


@pytest.fixture
def vault(tmp_path: Path) -> ObsidianVault:
    return ObsidianVault(tmp_path / "vault")


@pytest.fixture
def ontology(tmp_path: Path) -> Iterator[OntologyStore]:
    store = OntologyStore(tmp_path / "ontology.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def compiler_env(
    tmp_path: Path,
) -> Iterator[tuple[ObsidianVault, OntologyStore, WikiCompiler]]:
    vault = ObsidianVault(tmp_path / "vault")
    store = OntologyStore(tmp_path / "ontology.db")
    compiler = WikiCompiler(vault, store)
    try:
        yield vault, store, compiler
    finally:
        store.close()


def _write_note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------- vault
def test_initialize_creates_zone_directories(vault: ObsidianVault) -> None:
    for zone in ("wiki", "raw", "notes", "daily"):
        assert (vault.root / zone).is_dir()
    assert (vault.root / "wiki" / "proposals").is_dir()
    assert slugify("  Hello, World!  ") == "hello-world"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../secret.md",
        "notes/../../secret.md",
        "/etc/passwd",
        "C:secret.md",
    ],
)
def test_relative_path_traversal_is_rejected(
    vault: ObsidianVault, bad_path: str
) -> None:
    with pytest.raises(VaultSecurityError):
        vault.read_note(bad_path)


def test_absolute_path_and_traversal_writes_are_rejected(
    tmp_path: Path, vault: ObsidianVault
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(VaultSecurityError):
        vault.read_note(str(outside))
    with pytest.raises(VaultSecurityError):
        vault.write_wiki_page("../evil", "Evil", "body")
    assert not (tmp_path / "evil.md").exists()
    assert not (vault.root.parent / "evil.md").exists()


def _create_escape_link(link: Path, outside: Path) -> None:
    """Create a symlink, or a junction on Windows when symlinks need privileges."""

    try:
        link.symlink_to(outside, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"symlinks unavailable: {exc}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"symlink and junction creation unavailable: {result.stderr}")


def test_symlink_escape_is_rejected(tmp_path: Path, vault: ObsidianVault) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_note(outside / "secret.md", "# Secret\n\nclassified")
    _create_escape_link(vault.root / "escape", outside)
    with pytest.raises(VaultSecurityError):
        vault.read_note("escape/secret.md")


def test_notes_zone_only_changes_through_proposals(vault: ObsidianVault) -> None:
    manual = vault.root / "notes" / "manual.md"
    _write_note(manual, "# Manual\n\noriginal body\n")
    proposal = vault.propose_note_edit(
        "notes/manual.md", "Clarify the first paragraph", "rewritten body"
    )
    assert proposal.parent == vault.root / "wiki" / "proposals"
    assert proposal.is_file()
    proposal_text = proposal.read_text(encoding="utf-8")
    assert "notes/manual.md" in proposal_text
    assert "Clarify the first paragraph" in proposal_text
    assert "rewritten body" in proposal_text
    assert "note_edit_proposal" in proposal_text
    assert manual.read_text(encoding="utf-8") == "# Manual\n\noriginal body\n"

    with pytest.raises(VaultSecurityError):
        vault.propose_note_edit("wiki/manual.md", "not a note", "body")
    with pytest.raises(ValueError):
        vault.propose_note_edit("notes/manual.md", "   ", "body")


def test_raw_archive_is_append_only(vault: ObsidianVault) -> None:
    first = vault.archive_raw(
        "weekly-report",
        "Weekly Report",
        "hello raw",
        source_uri="mailto:alice@163.com",
        metadata={"week": 42},
    )
    assert first == vault.root / "raw" / "weekly-report.md"
    text = first.read_text(encoding="utf-8")
    assert "hello raw" in text
    assert "mailto:alice@163.com" in text

    same = vault.archive_raw("weekly-report", "Weekly Report", "hello raw")
    assert same == first

    with pytest.raises(VaultSecurityError):
        vault.archive_raw("weekly-report", "Weekly Report", "changed raw")
    assert "changed raw" not in first.read_text(encoding="utf-8")
    with pytest.raises(VaultSecurityError):
        vault.archive_raw("../escape", "Escape", "body")


def test_parse_wikilinks_variants_and_code_blocks() -> None:
    content = (
        "[[Target]] [[Target|Alias]] [[Target#Heading]] [[Doc.md#H|D]]\n"
        "```\n[[InFence]]\n```\n"
        "`[[InlineCode]]`\n"
    )
    links = parse_wikilinks(content)
    triples = [(link["target"], link["alias"], link["heading"]) for link in links]
    assert ("Target", None, None) in triples
    assert ("Target", "Alias", None) in triples
    assert ("Target", None, "Heading") in triples
    assert ("Doc", "D", "H") in triples
    assert all("InFence" not in str(link) for link in links)
    assert all("InlineCode" not in str(link) for link in links)


def test_read_note_and_backlinks(vault: ObsidianVault) -> None:
    vault.write_wiki_page(
        "target-page", "Target Page", "Body with no outgoing links.", tags=["work"]
    )
    vault.write_wiki_page(
        "source-page", "Source Page", "See [[target-page]] for details."
    )
    vault.write_wiki_page(
        "alias-source", "Alias Source", "See [[Target-Page|the page]]."
    )

    note = vault.read_note("wiki/target-page.md")
    assert note["path"] == "wiki/target-page.md"
    assert note["title"] == "Target Page"
    assert note["tags"] == ["work"]
    assert note["links"] == []
    assert note["zone"] == "wiki"

    source = vault.read_note("wiki/source-page.md")
    assert source["links"] == [
        {"target": "target-page", "alias": None, "heading": None}
    ]

    backlink_paths = {item["path"] for item in vault.backlinks("target-page")}
    assert backlink_paths == {"wiki/source-page.md", "wiki/alias-source.md"}
    assert len(vault.backlinks("Target-Page.md")) == 2
    assert vault.backlinks("missing-page") == []
    with pytest.raises(ValueError):
        vault.backlinks("   ")


def test_search_matches_title_tags_content_and_zone(vault: ObsidianVault) -> None:
    vault.write_wiki_page(
        "project-alpha", "Project Alpha", "Deadline is Friday.", tags=["work"]
    )
    vault.write_wiki_page("project-beta", "Project Beta", "Nothing here.")
    _write_note(
        vault.root / "notes" / "manual.md",
        "---\ntitle: Manual Note\ntags: [archive]\n---\n\nalpha reference\n",
    )
    vault.archive_raw("raw-alpha", "Snapshot", "alpha raw body")

    all_hits = vault.search("alpha")
    assert {hit["path"] for hit in all_hits} == {
        "wiki/project-alpha.md",
        "notes/manual.md",
        "raw/raw-alpha.md",
    }
    assert all_hits[0]["path"] == "wiki/project-alpha.md"
    assert all_hits[0]["score"] > 0
    assert all_hits[0]["snippet"]

    notes_hits = vault.search("alpha", zone="notes")
    assert [hit["path"] for hit in notes_hits] == ["notes/manual.md"]

    tag_hits = vault.search("work", zone="wiki")
    assert [hit["path"] for hit in tag_hits] == ["wiki/project-alpha.md"]

    assert vault.search("zzz-not-there") == []
    assert vault.search("alpha", limit=0) == []
    with pytest.raises(ValueError):
        vault.search("alpha", zone="unknown")


def test_append_daily_creates_and_appends(vault: ObsidianVault) -> None:
    first = vault.append_daily("finished migration", date_str="2026-01-02")
    assert first == vault.root / "daily" / "2026-01-02.md"
    assert "# 2026-01-02" in first.read_text(encoding="utf-8")

    vault.append_daily("wrote tests", date_str="2026-01-02")
    text = first.read_text(encoding="utf-8")
    assert "finished migration" in text
    assert "wrote tests" in text
    assert text.count("# 2026-01-02") == 1

    with pytest.raises(ValueError):
        vault.append_daily("bad date", date_str="2026-13-40")
    with pytest.raises(ValueError):
        vault.append_daily("bad format", date_str="01/02/2026")
    with pytest.raises(ValueError):
        vault.append_daily("   ", date_str="2026-01-02")


# ------------------------------------------------------------------ ontology
def test_ontology_upsert_entity_is_idempotent(ontology: OntologyStore) -> None:
    alice = ontology.upsert_entity(
        "Person", "Alice", "person:alice", {"email": "alice@163.com"}
    )
    assert set(alice) >= {
        "id",
        "entity_type",
        "name",
        "canonical_key",
        "attrs",
        "updated_at",
    }
    assert alice["entity_type"] == "Person"
    assert alice["attrs"] == {"email": "alice@163.com"}

    again = ontology.upsert_entity(
        "Person", "Alice Chen", "person:alice", {"timezone": "Asia/Shanghai"}
    )
    assert again["id"] == alice["id"]
    assert again["name"] == "Alice Chen"
    assert again["attrs"] == {"timezone": "Asia/Shanghai"}
    assert len(ontology.query_entities(entity_type="Person")) == 1

    derived = ontology.upsert_entity("Project", "Smart CS")
    assert derived["canonical_key"] == "project:smart-cs"

    with pytest.raises(ValueError):
        ontology.upsert_entity("Alien", "X")
    with pytest.raises(ValueError):
        ontology.upsert_entity("Person", "   ")


def test_ontology_cross_platform_alias_resolution(ontology: OntologyStore) -> None:
    alice = ontology.upsert_entity("Person", "Alice", "person:alice")
    aliases = [
        ("mailto:alice@163.com", "mail"),
        ("github:alice", "github"),
        ("feishu:ou_123", "feishu"),
    ]
    for alias_key, source in aliases:
        stored = ontology.link_alias(alias_key, alice["id"], source)
        assert stored["entity_id"] == alice["id"]
        assert stored["confidence"] == 1.0

    for alias_key, _ in aliases:
        resolved = ontology.resolve_by_alias(alias_key)
        assert resolved is not None
        assert resolved["id"] == alice["id"]
        assert resolved["entity_type"] == "Person"

    assert ontology.resolve_by_alias("GITHUB:Alice")["id"] == alice["id"]
    assert ontology.resolve_by_alias("mailto:nobody@example.com") is None
    assert ontology.resolve_by_alias("   ") is None

    bob = ontology.upsert_entity("Person", "Bob", "person:bob")
    ontology.link_alias("github:alice", bob["id"], "github")
    assert ontology.resolve_by_alias("github:alice")["id"] == bob["id"]

    with pytest.raises(ValueError):
        ontology.link_alias("github:ghost", "missing-entity", "github")
    assert len(ontology.query_entities(text="feishu:ou_123")) == 1


def test_ontology_relations_neighbors_and_uniqueness(
    ontology: OntologyStore,
) -> None:
    note = ontology.upsert_entity("NotePage", "Meeting notes", "notepage:meeting")
    alice = ontology.upsert_entity("Person", "Alice", "person:alice")
    project = ontology.upsert_entity("Project", "Smart CS", "project:smartcs")

    relation = ontology.add_relation(
        note["id"], "mentions", alice["id"], evidence_uri="wiki/meeting.md"
    )
    assert relation["rel_type"] == "mentions"
    duplicate = ontology.add_relation(
        note["id"], "mentions", alice["id"], evidence_uri="mailto:alice@163.com"
    )
    assert duplicate["id"] == relation["id"]
    assert duplicate["evidence_uri"] == "mailto:alice@163.com"
    ontology.add_relation(note["id"], "relates_to", project["id"])

    outgoing = ontology.get_neighbors(note["id"], direction="out")
    assert len(outgoing) == 2
    assert {item["rel_type"] for item in outgoing} == {"mentions", "relates_to"}

    mentions = ontology.get_neighbors(note["id"], rel_type="mentions")
    assert len(mentions) == 1
    assert mentions[0]["entity"]["id"] == alice["id"]
    assert mentions[0]["direction"] == "out"
    assert mentions[0]["evidence_uri"] == "mailto:alice@163.com"

    incoming = ontology.get_neighbors(alice["id"], direction="in")
    assert len(incoming) == 1
    assert incoming[0]["entity"]["id"] == note["id"]
    assert incoming[0]["direction"] == "in"

    both = ontology.get_neighbors(note["id"], direction="both")
    assert len(both) == 2
    assert ontology.get_neighbors(alice["id"], direction="out") == []
    assert ontology.get_neighbors("missing") == []

    with pytest.raises(ValueError):
        ontology.get_neighbors(note["id"], direction="sideways")
    with pytest.raises(ValueError):
        ontology.add_relation(note["id"], "mentions", "missing")
    with pytest.raises(ValueError):
        ontology.add_relation(note["id"], "   ", alice["id"])


def test_ontology_cascades_on_entity_delete(ontology: OntologyStore) -> None:
    alice = ontology.upsert_entity("Person", "Alice", "person:alice")
    project = ontology.upsert_entity("Project", "Smart CS", "project:smartcs")
    ontology.link_alias("github:alice", alice["id"], "github")
    ontology.add_relation(project["id"], "mentions", alice["id"])

    assert ontology.delete_entity(alice["id"]) is True
    assert ontology.get_entity(alice["id"]) is None
    assert ontology.resolve_by_alias("github:alice") is None
    assert ontology.get_neighbors(project["id"], direction="out") == []
    assert ontology.delete_entity(alice["id"]) is False


def test_ontology_query_entities_filters_and_limit(ontology: OntologyStore) -> None:
    ontology.upsert_entity("Person", "Alice", "person:alice")
    ontology.upsert_entity("Person", "Bob", "person:bob")
    ontology.upsert_entity("Project", "Alpha Project", "project:alpha")

    people = ontology.query_entities(entity_type="Person")
    assert [item["name"] for item in people] == ["Alice", "Bob"]
    assert [item["name"] for item in ontology.query_entities(text="alpha")] == [
        "Alpha Project"
    ]
    assert len(ontology.query_entities(limit=1)) == 1
    assert ontology.query_entities(entity_type="Project", text="alice") == []
    with pytest.raises(ValueError):
        ontology.query_entities(entity_type="Alien")
    with pytest.raises(ValueError):
        ontology.query_entities(limit=-1)

    assert "NotePage" in ALLOWED_ENTITY_TYPES


def test_ontology_persists_across_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.db"
    with OntologyStore(db_path) as store:
        entity = store.upsert_entity("Person", "Alice", "person:alice")
        store.link_alias("mailto:alice@163.com", entity["id"], "mail")
    with OntologyStore(db_path) as reopened:
        resolved = reopened.resolve_by_alias("mailto:alice@163.com")
        assert resolved is not None
        assert resolved["id"] == entity["id"]


# ------------------------------------------------------------- wiki compiler
def test_ingest_document_compiles_wiki_index_log_and_ontology(
    compiler_env: tuple[ObsidianVault, OntologyStore, WikiCompiler],
) -> None:
    vault, store, compiler = compiler_env
    result = compiler.ingest_document(
        source_uri="https://example.test/issues/42",
        title="Fix login flow",
        raw_content="raw body text",
        summary_markdown="# Summary\n\nLogin fixed.",
        slug="fix-login-flow",
        related_entities=[
            {
                "entity_type": "Person",
                "name": "Alice",
                "canonical_key": "person:alice",
                "aliases": ["mailto:alice@163.com", "github:alice"],
                "relation": "mentions",
            }
        ],
    )

    assert result["slug"] == "fix-login-flow"
    assert result["raw_path"].is_file()
    assert result["wiki_path"] == vault.root / "wiki" / "fix-login-flow.md"
    assert result["index_path"] == vault.root / "wiki" / "index.md"
    assert result["log_path"] == vault.root / "wiki" / "log.md"

    raw_text = result["raw_path"].read_text(encoding="utf-8")
    assert "raw body text" in raw_text
    assert "https://example.test/issues/42" in raw_text

    wiki_text = result["wiki_path"].read_text(encoding="utf-8")
    assert "Login fixed." in wiki_text
    assert "raw/fix-login-flow.md" in wiki_text
    assert "https://example.test/issues/42" in wiki_text

    index_text = result["index_path"].read_text(encoding="utf-8")
    assert "[[fix-login-flow|Fix login flow]]" in index_text
    assert result["log_path"].read_text(encoding="utf-8").count("ingested") == 1

    note_entity = store.get_entity(result["note_entity_id"])
    assert note_entity is not None
    assert note_entity["entity_type"] == "NotePage"
    alice = store.resolve_by_alias("mailto:alice@163.com")
    assert alice is not None
    assert alice["entity_type"] == "Person"
    neighbors = store.get_neighbors(
        note_entity["id"], rel_type="mentions", direction="out"
    )
    assert [item["entity"]["id"] for item in neighbors] == [alice["id"]]

    second = compiler.ingest_document(
        source_uri="https://example.test/issues/43",
        title="Second document",
        raw_content="second raw",
        summary_markdown="second summary",
    )
    index_text = second["index_path"].read_text(encoding="utf-8")
    assert index_text.index("[[fix-login-flow|") < index_text.index(
        "[[second-document|"
    )
    assert len(result["log_path"].read_text(encoding="utf-8").splitlines()) >= 3

    with pytest.raises(VaultSecurityError):
        compiler.ingest_document(
            source_uri="https://example.test/issues/42",
            title="Fix login flow",
            raw_content="different body",
            summary_markdown="changed",
            slug="fix-login-flow",
        )


def test_ingest_without_ontology_still_compiles(vault: ObsidianVault) -> None:
    compiler = WikiCompiler(vault)
    result = compiler.ingest_document(
        "https://example.test/doc",
        "Plain doc",
        "raw",
        "summary",
        related_entities=[{"entity_type": "Person", "name": "Alice"}],
    )
    assert result["note_entity_id"] is None
    assert result["linked_entities"] == []
    assert result["wiki_path"].is_file()


def test_rebuild_index_is_deterministic_and_sorted(
    compiler_env: tuple[ObsidianVault, OntologyStore, WikiCompiler],
) -> None:
    vault, _, compiler = compiler_env
    vault.write_wiki_page("zeta", "Zeta", "z")
    vault.write_wiki_page("alpha", "Alpha", "a")

    first_path = compiler.rebuild_index()
    first = first_path.read_text(encoding="utf-8")
    assert first.index("[[alpha|Alpha]]") < first.index("[[zeta|Zeta]]")
    assert "## Pages (2)" in first
    assert "proposals" not in first

    second_path = compiler.rebuild_index()
    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") == first

    _write_note(vault.root / "notes" / "manual.md", "# Manual\n\nbody\n")
    vault.propose_note_edit("notes/manual.md", "reason", "new body")
    compiler.rebuild_index()
    assert "[[manual" not in compiler.rebuild_index().read_text(encoding="utf-8")


def test_lint_detects_broken_links_orphans_and_unindexed(
    compiler_env: tuple[ObsidianVault, OntologyStore, WikiCompiler],
) -> None:
    vault, _, compiler = compiler_env
    vault.write_wiki_page("linked-page", "Linked Page", "Body.")
    vault.write_wiki_page("orphan", "Orphan", "Broken link: [[Missing Page]].")
    vault.write_wiki_page("hub", "Hub", "[[linked-page]] and [[orphan]].")
    _write_note(
        vault.root / "notes" / "manual.md",
        "# Manual\n\n[[linked-page]] [[orphan]] [[hub]]\n",
    )

    compiler.rebuild_index()
    report = compiler.lint_wiki()
    assert report["broken_links"] == [
        {"path": "wiki/orphan.md", "target": "Missing Page"}
    ]
    assert report["orphan_pages"] == []
    assert report["unindexed_pages"] == []

    vault.write_wiki_page("lonely", "Lonely", "No incoming links.")
    report = compiler.lint_wiki()
    assert report["orphan_pages"] == ["wiki/lonely.md"]
    assert report["unindexed_pages"] == ["wiki/lonely.md"]
    assert report["broken_links"] == [
        {"path": "wiki/orphan.md", "target": "Missing Page"}
    ]

    compiler.rebuild_index()
    report = compiler.lint_wiki()
    assert report["orphan_pages"] == ["wiki/lonely.md"]
    assert report["unindexed_pages"] == []


def test_lint_before_index_reports_every_page(
    compiler_env: tuple[ObsidianVault, OntologyStore, WikiCompiler],
) -> None:
    vault, _, compiler = compiler_env
    vault.write_wiki_page("one", "One", "body")
    vault.write_wiki_page("two", "Two", "body")
    report = compiler.lint_wiki()
    assert report["unindexed_pages"] == ["wiki/one.md", "wiki/two.md"]
    assert report["orphan_pages"] == ["wiki/one.md", "wiki/two.md"]
    assert report["broken_links"] == []
