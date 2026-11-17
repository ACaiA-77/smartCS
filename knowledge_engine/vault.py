"""Sandboxed Obsidian vault access with zone-aware write policy."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_DROP_RE = re.compile(r"[^\w-]+", re.UNICODE)
_HYPHEN_RUN_RE = re.compile(r"-{2,}")

ALLOWED_ZONES = ("wiki", "raw", "notes", "daily")
_NOTE_ZONES = ("wiki", "notes", "daily")
_LINK_ZONES = ("wiki", "notes", "raw")


class VaultSecurityError(ValueError):
    """Raised when a vault operation would leave the sandbox or break a zone rule."""


def slugify(value: str) -> str:
    """Return a filesystem-safe, lowercase slug for a title or identifier."""

    normalized = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    slug = _SLUG_DROP_RE.sub("-", normalized)
    slug = _HYPHEN_RUN_RE.sub("-", slug).strip("-_.")
    return slug or "untitled"


def _normalize_target(value: str) -> str:
    target = str(value).strip().replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    if target.casefold().endswith(".md"):
        target = target[:-3]
    return target.strip()


def parse_wikilinks(content: str) -> list[dict[str, str | None]]:
    """Parse ``[[target]]``, ``[[target|alias]]`` and ``[[target#heading]]`` links.

    Fenced and inline code spans are ignored so documentation examples do not
    turn into graph edges.
    """

    text = _FENCED_CODE_RE.sub("", str(content or ""))
    text = _INLINE_CODE_RE.sub("", text)
    links: list[dict[str, str | None]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for match in _WIKILINK_RE.finditer(text):
        inner = match.group(1).strip()
        if not inner:
            continue
        target_part, _, alias_part = inner.partition("|")
        alias = alias_part.strip() or None
        target_part, _, heading_part = target_part.partition("#")
        heading = heading_part.strip() or None
        target = _normalize_target(target_part)
        if not target:
            continue
        key = (target, alias, heading)
        if key in seen:
            continue
        seen.add(key)
        links.append({"target": target, "alias": alias, "heading": heading})
    return links


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        if text[0] == '"':
            try:
                return str(json.loads(text))
            except json.JSONDecodeError:
                return text[1:-1]
        return text[1:-1]
    return text


def _split_inline_list(value: str) -> list[str]:
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    meta: dict[str, Any] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [
                _unquote(item) for item in _split_inline_list(value[1:-1])
            ]
        else:
            meta[key] = _unquote(value)
    return meta, text[match.end() :]


def _render_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _render_frontmatter(meta: Mapping[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(_render_scalar(item) for item in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {_render_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = _split_inline_list(value)
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        items = [str(value).strip()]
    return [item for item in items if item]


def _title_for(meta: Mapping[str, Any], body: str, fallback: str) -> str:
    raw_title = meta.get("title")
    if isinstance(raw_title, list):
        raw_title = raw_title[0] if raw_title else None
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _snippet(body: str, needle: str) -> str:
    lowered = body.casefold()
    index = lowered.find(needle)
    if index < 0:
        window = body[:160]
    else:
        window = body[max(0, index - 60) : index + 100]
    text = " ".join(window.split())
    if index > 60:
        text = "…" + text
    if index >= 0 and index + 100 < len(body):
        text += "…"
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ObsidianVault:
    """Zone-aware, path-validated access to one Obsidian vault directory.

    ``notes/`` is read-only for the agent, ``raw/`` only accepts immutable
    snapshots and ``wiki/`` is the agent-compiled workspace. Every path is
    resolved and checked against the vault root before use.
    """

    def __init__(self, vault_root: str | Path) -> None:
        raw_root = Path(vault_root).expanduser()
        raw_root.mkdir(parents=True, exist_ok=True)
        self.root = raw_root.resolve()
        self.initialize()

    def initialize(self) -> None:
        """Create the four zone directories (and the proposal folder) if missing."""

        for zone in ALLOWED_ZONES:
            (self.root / zone).mkdir(parents=True, exist_ok=True)
        (self.root / "wiki" / "proposals").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _resolve(self, rel_path: str | Path, *, for_write: bool = False) -> Path:
        raw = str(rel_path).strip()
        if not raw or "\x00" in raw:
            raise VaultSecurityError("vault path must be a non-empty relative path")
        candidate = Path(raw)
        drive_path = bool(re.match(r"^[A-Za-z]:", raw))
        if (
            drive_path
            or candidate.is_absolute()
            or candidate.anchor
            or raw[:1] in {"/", "\\"}
        ):
            raise VaultSecurityError(f"absolute vault paths are not allowed: {raw}")
        if any(part == ".." for part in candidate.parts):
            raise VaultSecurityError(f"path traversal is not allowed: {raw}")
        unresolved = self.root / candidate
        if not unresolved.is_relative_to(self.root):
            raise VaultSecurityError(f"path escapes the vault: {raw}")
        resolved = unresolved.resolve()
        if not resolved.is_relative_to(self.root):
            raise VaultSecurityError(f"symlink escapes the vault: {raw}")
        if for_write:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _zone_of(self, path: Path) -> str:
        parts = path.relative_to(self.root).parts
        return parts[0] if len(parts) > 1 else ""

    @staticmethod
    def _normalize_slug(value: str) -> str:
        text = str(value).strip()
        if not text or any(ch in text for ch in ("/", "\\", "\x00")) or ".." in text:
            raise VaultSecurityError(f"unsafe slug: {value!r}")
        slug = slugify(text)
        if slug in {"", ".", ".."}:
            raise VaultSecurityError(f"unsafe slug: {value!r}")
        return slug

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")

    def _iter_markdown(self, zones: Iterable[str]) -> Iterable[Path]:
        for zone in zones:
            base = self.root / zone
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.md")):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.root)
                if rel.parts[:2] == ("wiki", "proposals"):
                    continue
                try:
                    resolved_path = path.resolve()
                except OSError:
                    continue
                if not resolved_path.is_relative_to(self.root):
                    continue
                yield path

    def list_notes(self, zone: str | None = None) -> list[Path]:
        """Return markdown files in a zone (all zones when ``zone`` is None)."""

        if zone is not None and zone not in ALLOWED_ZONES:
            raise ValueError(f"unknown vault zone: {zone}")
        zones = (zone,) if zone else ALLOWED_ZONES
        paths = list(self._iter_markdown(zones))
        return sorted(paths, key=lambda item: self._relative(item).casefold())

    # ------------------------------------------------------------------ reads
    def read_note(self, rel_path: str) -> dict[str, Any]:
        """Read one note and return its path, title, content, links and tags."""

        path = self._resolve(rel_path)
        if not path.exists() and not path.suffix:
            candidate = path.with_name(path.name + ".md")
            if candidate.exists():
                path = candidate
        if not path.is_file():
            raise FileNotFoundError(f"note not found: {rel_path}")
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return {
            "path": self._relative(path),
            "title": _title_for(meta, body, path.stem),
            "content": text,
            "links": parse_wikilinks(body),
            "tags": _normalize_tags(meta.get("tags")),
            "zone": self._zone_of(path),
        }

    def search(
        self, query: str, zone: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Case-insensitive title/tag/content search with deterministic ranking."""

        if zone is not None and zone not in ALLOWED_ZONES:
            raise ValueError(f"unknown vault zone: {zone}")
        needle = str(query or "").strip().casefold()
        if not needle or limit <= 0:
            return []
        zones = (zone,) if zone else ALLOWED_ZONES
        results: list[dict[str, Any]] = []
        for path in self._iter_markdown(zones):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            title = _title_for(meta, body, path.stem)
            tags = _normalize_tags(meta.get("tags"))
            score = 0
            if needle in title.casefold():
                score += 3
            if any(needle in tag.casefold() for tag in tags):
                score += 2
            if needle in body.casefold():
                score += 1
            if score == 0:
                continue
            results.append(
                {
                    "path": self._relative(path),
                    "title": title,
                    "zone": self._zone_of(path),
                    "tags": tags,
                    "score": score,
                    "snippet": _snippet(body, needle),
                }
            )
        results.sort(key=lambda item: (-int(item["score"]), str(item["path"]).casefold()))
        return results[:limit]

    @staticmethod
    def _targets_match(left: str, right: str) -> bool:
        first = _normalize_target(left).casefold()
        second = _normalize_target(right).casefold()
        if not first or not second:
            return False
        if first == second:
            return True
        if "/" not in first and "/" in second:
            return second.rsplit("/", 1)[-1] == first
        return False

    def backlinks(self, target: str) -> list[dict[str, Any]]:
        """Return notes (wiki/notes/daily) that link to ``target``."""

        needle = _normalize_target(target)
        if not needle:
            raise ValueError("backlink target must not be empty")
        results: list[dict[str, Any]] = []
        for path in self._iter_markdown(_NOTE_ZONES):
            text = path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            if not any(
                self._targets_match(link["target"] or "", needle)
                for link in parse_wikilinks(body)
            ):
                continue
            results.append(
                {
                    "path": self._relative(path),
                    "title": _title_for(meta, body, path.stem),
                    "zone": self._zone_of(path),
                }
            )
        results.sort(key=lambda item: str(item["path"]).casefold())
        return results

    def link_exists(self, target: str, zones: Iterable[str] = _LINK_ZONES) -> bool:
        """Return whether a wikilink target resolves to any note in ``zones``."""

        normalized = _normalize_target(target)
        if not normalized:
            return False
        needle = normalized.casefold()
        for path in self._iter_markdown(zones):
            rel = self._relative(path)
            rel_without_ext = rel[:-3] if rel.casefold().endswith(".md") else rel
            if needle in {rel_without_ext.casefold(), path.stem.casefold()}:
                return True
        return False

    # ----------------------------------------------------------------- writes
    def archive_raw(
        self,
        slug: str,
        title: str,
        content: str,
        source_uri: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        """Create an immutable raw snapshot; identical content is idempotent."""

        clean = self._normalize_slug(slug)
        path = self._resolve(f"raw/{clean}.md", for_write=True)
        body = str(content).replace("\r\n", "\n").rstrip("\n")
        if path.exists():
            _, existing_body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if existing_body.strip("\n") != body.strip("\n"):
                raise VaultSecurityError(
                    f"raw snapshots are append-only; refusing to overwrite {clean}.md"
                )
            return path
        meta: dict[str, Any] = {
            "title": title,
            "type": "raw",
            "archived_at": _utc_now(),
        }
        if source_uri:
            meta["source_uri"] = source_uri
        if metadata:
            meta["metadata"] = json.dumps(
                dict(metadata), ensure_ascii=False, sort_keys=True
            )
        self._write_text(path, _render_frontmatter(meta) + "\n" + body + "\n")
        return path

    def append_daily(self, entry: str, date_str: str | None = None) -> Path:
        """Append one entry to ``daily/YYYY-MM-DD.md``, creating the page if needed."""

        text = str(entry).strip("\n")
        if not text.strip():
            raise ValueError("daily entry must not be empty")
        if date_str is None:
            day = date.today()
        else:
            candidate = str(date_str).strip()
            if not _DATE_RE.match(candidate):
                raise ValueError(f"invalid daily date: {date_str!r}")
            try:
                day = datetime.strptime(candidate, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError(f"invalid daily date: {date_str!r}") from exc
        path = self._resolve(f"daily/{day.isoformat()}.md", for_write=True)
        if not path.exists():
            self._write_text(path, f"# {day.isoformat()}\n\n")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text.rstrip("\n") + "\n")
        return path

    def write_wiki_page(
        self,
        slug: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
    ) -> Path:
        """Write one compiled page under ``wiki/`` (agent-owned zone)."""

        clean = self._normalize_slug(slug)
        path = self._resolve(f"wiki/{clean}.md", for_write=True)
        meta: dict[str, Any] = {"title": title, "type": "wiki"}
        if tags:
            meta["tags"] = list(tags)
        body = str(content).replace("\r\n", "\n").rstrip("\n")
        self._write_text(path, _render_frontmatter(meta) + "\n" + body + "\n")
        return path

    def append_wiki_log(self, entry: str) -> Path:
        """Append one line to the compiler-owned ``wiki/log.md``."""

        text = str(entry).strip("\n")
        if not text.strip():
            raise ValueError("wiki log entry must not be empty")
        path = self._resolve("wiki/log.md", for_write=True)
        if not path.exists():
            self._write_text(path, "# Wiki Log\n\n")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text.rstrip("\n") + "\n")
        return path

    def propose_note_edit(
        self, target_rel_path: str, rationale: str, proposed_content: str
    ) -> Path:
        """Save a structured edit proposal without ever writing to ``notes/``."""

        target = self._resolve(target_rel_path)
        target_rel = self._relative(target)
        if not target_rel.startswith("notes/"):
            raise VaultSecurityError(
                "note edit proposals may only target the notes/ zone"
            )
        reason = str(rationale).strip()
        if not reason:
            raise ValueError("proposal rationale must not be empty")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        proposal_name = f"{timestamp}-{slugify(target.stem)}.md"
        path = self._resolve(f"wiki/proposals/{proposal_name}", for_write=True)
        meta = {
            "type": "note_edit_proposal",
            "target": target_rel,
            "created_at": _utc_now(),
            "status": "proposed",
        }
        body = (
            f"\n# Proposed edit: {target_rel}\n\n"
            "## Rationale\n\n"
            f"{reason}\n\n"
            "## Proposed content\n\n"
            "~~~markdown\n"
            f"{str(proposed_content).replace(chr(13) + chr(10), chr(10)).rstrip()}\n"
            "~~~\n"
        )
        self._write_text(path, _render_frontmatter(meta) + body)
        return path
