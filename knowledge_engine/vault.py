"""Sandboxed Obsidian vault access with zone-aware write policy."""

from __future__ import annotations

import json
import os
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
SOURCE_ZONE = "source"
READ_ZONES = ALLOWED_ZONES + (SOURCE_ZONE,)
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
    """Zone-aware, path-validated access to an Obsidian vault workspace.

    ``notes/`` is read-only for the agent, ``raw/`` only accepts immutable
    snapshots and ``wiki/`` is the agent-compiled workspace. An optional
    ``source_root`` mounts an external non-four-zone Obsidian vault strictly
    read-only under the virtual ``source/`` prefix without initializing or
    writing into it.
    """

    def __init__(
        self,
        vault_root: str | Path,
        *,
        source_root: str | Path | None = None,
        read_only: bool = False,
    ) -> None:
        self.read_only = bool(read_only)
        raw_root = Path(vault_root).expanduser()
        if self.read_only:
            if not raw_root.is_dir():
                raise FileNotFoundError(f"read-only vault directory does not exist: {vault_root}")
            self.root = raw_root.resolve()
            self.source_root: Path | None = None
            return

        raw_root.mkdir(parents=True, exist_ok=True)
        self.root = raw_root.resolve()
        self.initialize()

        if source_root is not None:
            raw_source = Path(source_root).expanduser()
            if not raw_source.is_dir():
                raise FileNotFoundError(
                    f"read-only source vault directory does not exist: {source_root}"
                )
            resolved_source = raw_source.resolve()
            if (
                resolved_source == self.root
                or resolved_source.is_relative_to(self.root)
                or self.root.is_relative_to(resolved_source)
            ):
                raise VaultSecurityError(
                    "managed vault root and read-only source root must not overlap"
                )
            self.source_root = resolved_source
        else:
            self.source_root = None

    def initialize(self) -> None:
        """Create the four zone directories (and the proposal folder) if missing."""
        if self.read_only:
            raise VaultSecurityError("read-only vault cannot be initialized")
        for zone in ALLOWED_ZONES:
            (self.root / zone).mkdir(parents=True, exist_ok=True)
        (self.root / "wiki" / "proposals").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    @staticmethod
    def _parse_relative_candidate(rel_path: str | Path) -> tuple[str, Path]:
        raw = str(rel_path).strip()
        if not raw or "\x00" in raw:
            raise VaultSecurityError("vault path must be a non-empty relative path")
        if raw[:1] in {"/", "\\"} or bool(re.match(r"^[A-Za-z]:", raw)):
            raise VaultSecurityError(f"absolute vault paths are not allowed: {raw}")
        normalized_raw = raw.replace("\\", "/")
        candidate = Path(normalized_raw)
        if candidate.is_absolute() or candidate.anchor:
            raise VaultSecurityError(f"absolute vault paths are not allowed: {raw}")
        raw_parts = [part for part in normalized_raw.split("/") if part]
        if any(part == ".." for part in raw_parts) or any(part == ".." for part in candidate.parts):
            raise VaultSecurityError(f"path traversal is not allowed: {raw}")
        if any(part.startswith(".") for part in raw_parts) or any(part.startswith(".") for part in candidate.parts):
            raise VaultSecurityError(f"hidden paths are not allowed: {raw}")
        return normalized_raw, candidate

    @staticmethod
    def _validate_resolved_under(
        base_root: Path, unresolved: Path, *, raw: str, for_write: bool = False
    ) -> Path:
        if not unresolved.is_relative_to(base_root):
            raise VaultSecurityError(f"path escapes the vault: {raw}")
        resolved = unresolved.resolve()
        if not resolved.is_relative_to(base_root):
            raise VaultSecurityError(f"symlink escapes the vault: {raw}")
        rel_parts = resolved.relative_to(base_root).parts
        if any(part.startswith(".") for part in rel_parts):
            raise VaultSecurityError(f"hidden paths are not allowed: {raw}")
        if for_write:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _resolve_parts(
        self, rel_path: str | Path, *, for_write: bool = False
    ) -> tuple[Path, Path, str]:
        raw, candidate = self._parse_relative_candidate(rel_path)
        if for_write and self.read_only:
            raise VaultSecurityError("read-only vault cannot be modified")
        if candidate.parts and candidate.parts[0] == SOURCE_ZONE:
            if self.read_only:
                if len(candidate.parts) < 2:
                    raise VaultSecurityError("source path must name a note")
                inner = Path(*candidate.parts[1:])
                resolved = self._validate_resolved_under(
                    self.root, self.root / inner, raw=raw, for_write=False
                )
                return self.root, resolved, raw
            if self.source_root is not None:
                if for_write:
                    raise VaultSecurityError("read-only source vault cannot be modified")
                if len(candidate.parts) < 2:
                    raise VaultSecurityError("source path must name a note")
                inner = Path(*candidate.parts[1:])
                resolved = self._validate_resolved_under(
                    self.source_root, self.source_root / inner, raw=raw, for_write=False
                )
                return self.source_root, resolved, raw
        resolved = self._validate_resolved_under(
            self.root, self.root / candidate, raw=raw, for_write=for_write
        )
        return self.root, resolved, raw

    def _resolve(self, rel_path: str | Path, *, for_write: bool = False) -> Path:
        _, resolved, _ = self._resolve_parts(rel_path, for_write=for_write)
        return resolved

    def _is_source_path(self, path: Path) -> bool:
        if self.read_only:
            return True
        if self.source_root is None:
            return False
        try:
            return path.is_relative_to(self.source_root) or path.resolve().is_relative_to(self.source_root)
        except OSError:
            return False

    def relative_path(self, path: Path) -> str:
        """Return the canonical vault-relative POSIX path (prefixed ``source/`` for read-only source)."""

        if self.read_only:
            return f"{SOURCE_ZONE}/{path.relative_to(self.root).as_posix()}"
        if self.source_root is not None and path.is_relative_to(self.source_root):
            return f"{SOURCE_ZONE}/{path.relative_to(self.source_root).as_posix()}"
        return path.relative_to(self.root).as_posix()

    def _relative(self, path: Path) -> str:
        return self.relative_path(path)

    def _zone_of(self, path: Path) -> str:
        if self._is_source_path(path):
            return SOURCE_ZONE
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

    @staticmethod
    def _iter_dir_markdown(
        base_root: Path, scan_dir: Path, *, skip_proposals: bool = False
    ) -> Iterable[Path]:
        if not scan_dir.is_dir() or scan_dir.is_symlink():
            return
        for cur, dirnames, filenames in os.walk(scan_dir, followlinks=False):
            cur_path = Path(cur)
            filtered_dirs = []
            for dirname in sorted(dirnames, key=str.casefold):
                if dirname.startswith("."):
                    continue
                sub = cur_path / dirname
                if sub.is_symlink():
                    continue
                try:
                    if not sub.resolve().is_relative_to(base_root):
                        continue
                except OSError:
                    continue
                if skip_proposals and sub.relative_to(base_root).parts[:2] == ("wiki", "proposals"):
                    continue
                filtered_dirs.append(dirname)
            dirnames[:] = filtered_dirs

            for filename in sorted(filenames, key=str.casefold):
                if filename.startswith(".") or not filename.casefold().endswith(".md"):
                    continue
                path = cur_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if not resolved.is_relative_to(base_root):
                    continue
                if any(part.startswith(".") for part in resolved.relative_to(base_root).parts):
                    continue
                yield path

    def _iter_markdown(self, zones: Iterable[str]) -> Iterable[Path]:
        for zone in zones:
            if zone == SOURCE_ZONE:
                if self.read_only:
                    yield from self._iter_dir_markdown(self.root, self.root)
                elif self.source_root is not None:
                    yield from self._iter_dir_markdown(self.source_root, self.source_root)
                continue
            if self.read_only:
                continue
            base = self.root / zone
            yield from self._iter_dir_markdown(
                self.root, base, skip_proposals=(zone == "wiki")
            )

    def _default_read_zones(self) -> tuple[str, ...]:
        if self.read_only:
            return (SOURCE_ZONE,)
        if self.source_root is not None:
            return ALLOWED_ZONES + (SOURCE_ZONE,)
        return ALLOWED_ZONES

    def list_notes(self, zone: str | None = None) -> list[Path]:
        """Return markdown files in a zone (all configured zones when ``zone`` is None)."""

        if zone is not None and zone not in READ_ZONES:
            raise ValueError(f"unknown vault zone: {zone}")
        zones = (zone,) if zone else self._default_read_zones()
        paths = list(self._iter_markdown(zones))
        return sorted(paths, key=lambda item: self._relative(item).casefold())

    # ------------------------------------------------------------------ reads
    def _resolve_with_md_fallback(self, base_root: Path, candidate: Path, *, raw: str) -> Path:
        resolved = self._validate_resolved_under(base_root, base_root / candidate, raw=raw)
        if not resolved.exists() and not resolved.suffix:
            candidate_md = resolved.with_name(resolved.name + ".md")
            if candidate_md.exists():
                resolved = self._validate_resolved_under(base_root, candidate_md, raw=raw)
        return resolved

    def read_note(self, rel_path: str) -> dict[str, Any]:
        """Read one note and return its path, title, content, links and tags."""

        base_root, path, raw = self._resolve_parts(rel_path)
        if not path.exists() and not path.suffix:
            candidate_md = path.with_name(path.name + ".md")
            if candidate_md.exists():
                path = self._validate_resolved_under(base_root, candidate_md, raw=raw)
        if not path.is_file() and self.source_root is not None and not self.read_only:
            _, candidate = self._parse_relative_candidate(rel_path)
            if candidate.parts and candidate.parts[0] not in (*ALLOWED_ZONES, SOURCE_ZONE):
                src_path = self._resolve_with_md_fallback(self.source_root, candidate, raw=raw)
                if src_path.is_file():
                    path = src_path
        if not path.is_file():
            raise FileNotFoundError(f"note not found: {rel_path}")
        text = path.read_text(encoding="utf-8-sig")
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

        if zone is not None and zone not in READ_ZONES:
            raise ValueError(f"unknown vault zone: {zone}")
        needle = str(query or "").strip().casefold()
        if not needle or limit <= 0:
            return []
        zones = (zone,) if zone else self._default_read_zones()
        results: list[dict[str, Any]] = []
        for path in self._iter_markdown(zones):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
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
    def _is_unsafe_link_target(value: str) -> bool:
        text = str(value or "").strip().replace("\\", "/")
        if not text or "\x00" in text:
            return True
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            return True
        parts = [part for part in text.split("/") if part]
        return any(part == ".." or part.startswith(".") for part in parts)

    @classmethod
    def _targets_match(cls, left: str, right: str) -> bool:
        first = _normalize_target(left).casefold()
        second = _normalize_target(right).casefold()
        if not first or not second:
            return False
        if cls._is_unsafe_link_target(first) or cls._is_unsafe_link_target(second):
            return False
        left_candidates = [first]
        if first.startswith(f"{SOURCE_ZONE}/"):
            left_candidates.append(first[len(SOURCE_ZONE) + 1 :])
        right_candidates = [second]
        if second.startswith(f"{SOURCE_ZONE}/"):
            right_candidates.append(second[len(SOURCE_ZONE) + 1 :])
        return any(
            l_cand == r_cand or r_cand.endswith(f"/{l_cand}")
            for l_cand in left_candidates
            for r_cand in right_candidates
        )

    def backlinks(self, target: str) -> list[dict[str, Any]]:
        """Return notes that link to ``target`` (never follows escaping/hidden targets)."""

        needle = _normalize_target(target)
        if not needle:
            raise ValueError("backlink target must not be empty")
        if self._is_unsafe_link_target(needle):
            raise VaultSecurityError(f"unsafe backlink target: {target!r}")
        if self.read_only:
            zones: tuple[str, ...] = (SOURCE_ZONE,)
        elif self.source_root is not None:
            zones = _NOTE_ZONES + (SOURCE_ZONE,)
        else:
            zones = _NOTE_ZONES
        results: list[dict[str, Any]] = []
        for path in self._iter_markdown(zones):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
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
        if not normalized or self._is_unsafe_link_target(normalized):
            return False
        needle = normalized.casefold()
        effective_zones = tuple(zones)
        if effective_zones == _LINK_ZONES and self.source_root is not None:
            effective_zones = _LINK_ZONES + (SOURCE_ZONE,)
        for path in self._iter_markdown(effective_zones):
            rel = self._relative(path)
            rel_without_ext = rel[:-3] if rel.casefold().endswith(".md") else rel
            candidates = {rel_without_ext.casefold(), path.stem.casefold()}
            if rel_without_ext.casefold().startswith(f"{SOURCE_ZONE}/"):
                candidates.add(rel_without_ext[len(SOURCE_ZONE) + 1 :].casefold())
            if needle in candidates:
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

        if self.read_only:
            raise VaultSecurityError("read-only vault cannot be modified")
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
