"""Parsing, structure-aware chunking, and source metadata handling."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import KnowledgeChunk, KnowledgeDocument

CHUNKING_VERSION = "structure-context-v1"
SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf"}
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_URL_RE = re.compile(r"https?://[^\s<>()]+")


# Keep the descriptive old name available to existing ingestion callers.
SourceDocument = KnowledgeDocument


@dataclass(frozen=True)
class ParsedSource:
    document: KnowledgeDocument
    document_id: str
    chunks: list[KnowledgeChunk] = field(default_factory=list)


def stable_document_id(domain: str, source: str) -> str:
    return hashlib.sha256(f"{domain}\n{source}".encode("utf-8")).hexdigest()[:24]


def stable_chunk_id(
    document_id: str,
    chunk_index: int,
    content: str,
    heading_path: list[str],
) -> str:
    payload = json.dumps(
        [document_id, chunk_index, content, heading_path],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def is_url_only_stub(text: str) -> bool:
    lines = [line.strip().lstrip("-* ").strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_URL_RE.fullmatch(line) for line in lines)


def urls_in(text: str) -> list[str]:
    return _URL_RE.findall(text)


def detect_language(text: str) -> str:
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def extract_pdf_text(path: Path) -> str:
    """Extract real PDF text. OCR is deliberately outside this pipeline."""

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


def read_source(path: Path, *, domain: str, metadata: dict[str, Any] | None = None, source: str = "") -> KnowledgeDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = extract_pdf_text(path)
    elif suffix in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"unsupported source type: {path.suffix}")

    embedded_metadata = _frontmatter(text) if suffix in {".md", ".txt"} else {}
    metadata = {**embedded_metadata, **(metadata or {})}
    text = _strip_frontmatter(text)
    if not text.strip():
        raise ValueError("empty_source")
    source_name = source or path.as_posix()
    title = str(metadata.get("title") or _title_from_text(text) or path.stem)
    source_url = str(metadata.get("source_url") or "")
    unresolved = suffix == ".txt" and is_url_only_stub(text)
    if unresolved and not source_url:
        source_url = urls_in(text)[0]
    return SourceDocument(
        source=source_name,
        content=text,
        domain=domain,
        title=title,
        source_url=source_url,
        source_type=str(metadata.get("source_type") or metadata.get("doc_type") or _default_doc_type(suffix)),
        language=str(metadata.get("language") or detect_language(text)),
        unresolved=unresolved,
        unresolved_reason="url_only_stub" if unresolved else "",
    )


def chunk_document(
    document: KnowledgeDocument,
    *,
    document_id: str | None = None,
    chunk_size: int = 900,
    overlap: int = 120,
) -> ParsedSource:
    """Chunk by heading sections first, then deterministic paragraph/sentence windows."""

    document_id = document_id or stable_document_id(document.domain, document.source)
    if document.unresolved or not document.content.strip():
        return ParsedSource(document, document_id)

    title = document.title or _title_from_text(document.content) or Path(document.source).stem
    sections = _sections(document.content, title)
    chunks: list[KnowledgeChunk] = []
    for heading_path, body in sections:
        for content in _windows(body, chunk_size=chunk_size, overlap=overlap):
            index = len(chunks)
            heading = list(heading_path)
            chunks.append(
                KnowledgeChunk(
                    chunk_id=stable_chunk_id(document_id, index, content, heading),
                    document_id=document_id,
                    domain=document.domain,
                    content=content,
                    retrieval_text=_contextual_text(
                        document.domain,
                        title,
                        heading,
                        document.source_type,
                        content,
                    ),
                    title=title,
                    heading_path=heading,
                    source=document.source,
                    source_url=document.source_url,
                    source_type=document.source_type,
                    language=document.language,
                    chunk_index=index,
                )
            )
    return ParsedSource(document, document_id, chunks)


def _sections(text: str, title: str) -> list[tuple[list[str], str]]:
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    body: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            if body:
                sections.append((list(path), body))
                body = []
            level = len(match.group(1))
            heading = match.group(2).strip()
            path = path[: level - 1]
            path.append(heading)
            continue
        body.append(line)
    if body:
        sections.append((list(path), body))
    if not sections:
        return [([title], text)]
    return [(heading_path or [title], "".join(lines)) for heading_path, lines in sections]


def _windows(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if len(clean) <= chunk_size:
        return [clean]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", clean) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + 2 + len(paragraph) > chunk_size:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        if len(paragraph) <= chunk_size:
            current = f"{current}\n\n{paragraph}".strip() if current else paragraph
            continue
        for sentence in _split_long(paragraph, chunk_size):
            if current and len(current) + 1 + len(sentence) > chunk_size:
                chunks.append(current)
                current = current[-overlap:] if overlap else ""
            current = f"{current} {sentence}".strip() if current else sentence
    if current:
        chunks.append(current)
    return chunks


def _split_long(text: str, size: int) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?])\s+", text) if item.strip()]
    if not sentences:
        return [text[i : i + size] for i in range(0, len(text), size)]
    result: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > size:
            result.append(current)
            current = ""
        if len(sentence) > size:
            if current:
                result.append(current)
                current = ""
            result.extend(sentence[i : i + size] for i in range(0, len(sentence), size))
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current:
        result.append(current)
    return result


def _contextual_text(
    domain: str,
    title: str,
    heading_path: list[str],
    source_type: str,
    content: str,
) -> str:
    section = " > ".join(heading_path) or title
    domain_label = {
        "apple_support": "Apple Support",
        "agent_engineering": "Agent Engineering",
    }.get(domain, domain)
    return (
        f"Domain: {domain_label} ({domain})\n"
        f"Document: {title}\n"
        f"Section: {section}\n"
        f"Source type: {source_type}\n\n{content}"
    )


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return ""


def _frontmatter(text: str) -> dict[str, str]:
    """Read the small quoted YAML subset emitted by fetch_knowledge_sources."""

    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}
    values: dict[str, str] = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(?:\"((?:\\.|[^\"])*)\"|(.*))$", line)
        if not match:
            continue
        value = match.group(2) if match.group(2) is not None else match.group(3).strip()
        values[match.group(1)] = value.replace('\\"', '"')
    return values


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if len(lines) < 3 or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[index + 1:]).lstrip("\r\n")
    return text


def _default_doc_type(suffix: str) -> str:
    return {".md": "markdown", ".txt": "text", ".pdf": "pdf"}.get(suffix, "document")
