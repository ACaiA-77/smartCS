from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import httpx


@dataclass(frozen=True)
class KnowledgeSource:
    id: str
    title: str
    url: str
    brand: str = ""
    doc_type: str = ""
    language: str = "zh-CN"
    output: str = ""


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    url: str
    output_path: str
    raw_html_path: str
    metadata_path: str
    status_code: int
    raw_chars: int
    clean_chars: int
    retrieved_at: str
    ok: bool = True
    error: str = ""


@dataclass(frozen=True)
class CleanedContent:
    markdown: str
    page_title: str
    cleaner: str


class KnowledgeHTMLExtractor(HTMLParser):
    """Small dependency-free extractor for public support pages."""

    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "button",
        "select",
        "option",
    }
    BLOCK_TAGS = {"p", "div", "section", "article", "main", "tr", "table"}
    HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._current_tag: str | None = None
        self._skip_depth = 0

    @property
    def page_title(self) -> str:
        return _clean_inline(" ".join(self.title_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._current_tag = tag
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.HEADING_TAGS:
            self.parts.append(f"\n\n{self.HEADING_TAGS[tag]} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.HEADING_TAGS or tag in {"p", "li", "tr", "section", "article"}:
            self.parts.append("\n")
        self._current_tag = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _clean_inline(data)
        if not text:
            return
        if self._current_tag == "title":
            self.title_parts.append(text)
        self.parts.append(text + " ")

    def markdown(self) -> str:
        raw = "".join(self.parts)
        lines = []
        seen = set()
        for line in raw.splitlines():
            cleaned = _clean_inline(line)
            if not cleaned:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if _is_boilerplate(cleaned):
                continue
            if cleaned in seen and len(cleaned) > 20:
                continue
            seen.add(cleaned)
            lines.append(cleaned)
        return "\n".join(lines).strip()


def _clean_inline(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_boilerplate(line: str) -> bool:
    lower = line.lower()
    boilerplate = [
        "cookie",
        "javascript",
        "版权所有",
        "copyright",
        "隐私政策",
        "使用条款",
        "site map",
        "sitemap",
    ]
    return any(token in lower for token in boilerplate)


def load_sources(config_path: str | Path) -> list[KnowledgeSource]:
    """Parse the small urls.yml schema without requiring PyYAML."""
    text = Path(config_path).read_text(encoding="utf-8")
    sources: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "sources:":
            continue
        if line.startswith("- "):
            if current:
                sources.append(current)
            current = {}
            line = line[2:].strip()
            if not line:
                continue
        if ":" not in line or current is None:
            continue
        key, value = line.split(":", 1)
        current[key.strip()] = value.strip().strip('"').strip("'")

    if current:
        sources.append(current)

    return [
        KnowledgeSource(
            id=item["id"],
            title=item.get("title", item["id"]),
            url=item["url"],
            brand=item.get("brand", ""),
            doc_type=item.get("doc_type", ""),
            language=item.get("language", "zh-CN"),
            output=item.get("output", f"{item['id']}.md"),
        )
        for item in sources
    ]


def clean_html_to_markdown(html: str, source: KnowledgeSource, retrieved_at: str) -> tuple[str, dict]:
    cleaned = extract_clean_markdown(html, source.url)
    body = cleaned.markdown
    page_title = cleaned.page_title or source.title
    frontmatter = {
        "title": source.title,
        "page_title": page_title,
        "source_id": source.id,
        "source_url": source.url,
        "brand": source.brand,
        "doc_type": source.doc_type,
        "language": source.language,
        "retrieved_at": retrieved_at,
    }
    markdown = _format_frontmatter(frontmatter) + "\n\n" + f"# {source.title}\n\n" + body + "\n"
    metadata = {
        **frontmatter,
        "raw_chars": len(html),
        "clean_chars": len(body),
        "cleaner": cleaned.cleaner,
    }
    return markdown, metadata


def extract_clean_markdown(html: str, url: str = "") -> CleanedContent:
    """Extract readable Markdown with production tools, then fallback locally."""
    for extractor in (
        _extract_with_trafilatura,
        _extract_with_readability_html2text,
        _extract_with_builtin_parser,
    ):
        cleaned = extractor(html, url)
        if cleaned.markdown:
            return cleaned
    return CleanedContent(markdown="", page_title="", cleaner="none")


def _extract_with_trafilatura(html: str, url: str = "") -> CleanedContent:
    try:
        import trafilatura
    except ImportError:
        return CleanedContent(markdown="", page_title="", cleaner="trafilatura_unavailable")

    extracted = trafilatura.extract(
        html,
        url=url or None,
        output_format="markdown",
        include_comments=False,
        include_images=False,
        include_links=True,
        include_tables=True,
        favor_precision=True,
    )
    markdown = _clean_markdown(extracted or "")
    title = _extract_title_from_html(html)
    return CleanedContent(markdown=markdown, page_title=title, cleaner="trafilatura")


def _extract_with_readability_html2text(html: str, url: str = "") -> CleanedContent:
    try:
        from readability import Document
    except ImportError:
        return CleanedContent(markdown="", page_title="", cleaner="readability_unavailable")

    try:
        doc = Document(html)
        summary_html = doc.summary(html_partial=True)
        title = _clean_inline(doc.short_title() or _extract_title_from_html(html))
    except Exception:
        return CleanedContent(markdown="", page_title="", cleaner="readability_failed")

    markdown = _html_to_markdown(summary_html)
    if not markdown:
        markdown = _builtin_html_to_markdown(summary_html)
    return CleanedContent(
        markdown=_clean_markdown(markdown),
        page_title=title,
        cleaner="readability-lxml+html2text",
    )


def _extract_with_builtin_parser(html: str, url: str = "") -> CleanedContent:
    body_html = _main_html_or_full_html(html)
    return CleanedContent(
        markdown=_builtin_html_to_markdown(body_html),
        page_title=_extract_title_from_html(html),
        cleaner="builtin_htmlparser",
    )


def _html_to_markdown(html: str) -> str:
    try:
        import html2text
    except ImportError:
        return ""

    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.ignore_links = False
    return converter.handle(html)


def _builtin_html_to_markdown(html: str) -> str:
    extractor = KnowledgeHTMLExtractor()
    extractor.feed(html)
    return extractor.markdown()


def _extract_title_from_html(html: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _clean_inline(re.sub(r"<[^>]+>", " ", match.group(1)))


def _clean_markdown(markdown: str) -> str:
    lines = []
    seen = set()
    for line in markdown.splitlines():
        cleaned = _clean_inline(line)
        if not cleaned:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _is_boilerplate(cleaned):
            continue
        if cleaned in seen and len(cleaned) > 20:
            continue
        seen.add(cleaned)
        lines.append(cleaned)
    return "\n".join(lines).strip()


def _main_html_or_full_html(html: str) -> str:
    match = re.search(r"<main\b[^>]*>(.*?)</main>", html, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1)
    return html


def _format_frontmatter(data: dict[str, str | int]) -> str:
    lines = ["---"]
    for key, value in data.items():
        safe_value = str(value).replace('"', '\\"')
        lines.append(f'{key}: "{safe_value}"')
    lines.append("---")
    return "\n".join(lines)


def fetch_sources(
    sources: Iterable[KnowledgeSource],
    raw_dir: str | Path,
    clean_dir: str | Path,
    metadata_dir: str | Path,
    review_path: str | Path,
    timeout: float = 30,
    min_clean_chars: int = 200,
) -> list[FetchResult]:
    raw_path = Path(raw_dir)
    clean_path = Path(clean_dir)
    meta_path = Path(metadata_dir)
    raw_path.mkdir(parents=True, exist_ok=True)
    clean_path.mkdir(parents=True, exist_ok=True)
    meta_path.mkdir(parents=True, exist_ok=True)

    results: list[FetchResult] = []
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "SmartCS-KnowledgeFetcher/1.0"},
    ) as client:
        for source in sources:
            retrieved_at = datetime.now(timezone.utc).isoformat()
            raw_file = raw_path / f"{source.id}.html"
            clean_file = clean_path / source.output
            metadata_file = meta_path / f"{source.id}.json"
            try:
                response = client.get(source.url)
                response.raise_for_status()
                html = response.text
            except Exception as exc:
                error_metadata = {
                    "title": source.title,
                    "source_id": source.id,
                    "source_url": source.url,
                    "brand": source.brand,
                    "doc_type": source.doc_type,
                    "language": source.language,
                    "retrieved_at": retrieved_at,
                    "error": str(exc),
                    "cleaner": "SmartCS KnowledgeHTMLExtractor",
                }
                metadata_file.write_text(
                    json.dumps(error_metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                results.append(
                    FetchResult(
                        source_id=source.id,
                        url=source.url,
                        output_path=str(clean_file),
                        raw_html_path=str(raw_file),
                        metadata_path=str(metadata_file),
                        status_code=0,
                        raw_chars=0,
                        clean_chars=0,
                        retrieved_at=retrieved_at,
                        ok=False,
                        error=str(exc),
                    )
                )
                continue

            markdown, metadata = clean_html_to_markdown(html, source, retrieved_at)

            raw_file.write_text(html, encoding="utf-8")
            metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

            if int(metadata["clean_chars"]) < min_clean_chars:
                if clean_file.exists():
                    clean_file.unlink()
                results.append(
                    FetchResult(
                        source_id=source.id,
                        url=source.url,
                        output_path=str(clean_file),
                        raw_html_path=str(raw_file),
                        metadata_path=str(metadata_file),
                        status_code=response.status_code,
                        raw_chars=len(html),
                        clean_chars=metadata["clean_chars"],
                        retrieved_at=retrieved_at,
                        ok=False,
                        error=f"clean content below threshold: {metadata['clean_chars']} < {min_clean_chars}",
                    )
                )
                continue

            clean_file.write_text(markdown, encoding="utf-8")

            results.append(
                FetchResult(
                    source_id=source.id,
                    url=source.url,
                    output_path=str(clean_file),
                    raw_html_path=str(raw_file),
                    metadata_path=str(metadata_file),
                    status_code=response.status_code,
                    raw_chars=len(html),
                    clean_chars=metadata["clean_chars"],
                    retrieved_at=retrieved_at,
                )
            )

    write_review_samples(results, review_path)
    return results


def write_review_samples(results: list[FetchResult], review_path: str | Path) -> None:
    path = Path(review_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# RAG 采集抽样校验", ""]
    lines.append("人工抽样时重点检查：是否混入导航/页脚、关键数字是否保留、来源 URL 是否正确。")
    lines.append("")
    for result in results:
        if not result.ok:
            lines.extend(
                [
                    f"## {result.source_id}",
                    "",
                    f"- URL: {result.url}",
                    f"- Error: {result.error}",
                    "",
                ]
            )
            continue
        output = Path(result.output_path)
        content = output.read_text(encoding="utf-8")
        preview = "\n".join(content.splitlines()[:28])
        lines.extend(
            [
                f"## {result.source_id}",
                "",
                f"- URL: {result.url}",
                f"- Raw chars: {result.raw_chars}",
                f"- Clean chars: {result.clean_chars}",
                "",
                "```md",
                preview,
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch public webpages into RAG-ready Markdown")
    parser.add_argument("--config", default="./knowledge_sources/urls.yml")
    parser.add_argument("--raw-dir", default="./knowledge_sources/raw_html")
    parser.add_argument("--clean-dir", default="./knowledge_base/generated")
    parser.add_argument("--metadata-dir", default="./knowledge_sources/metadata")
    parser.add_argument("--review-path", default="./knowledge_sources/review_samples.md")
    parser.add_argument("--manifest-path", default="./knowledge_sources/manifest.json")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--min-clean-chars", type=int, default=200)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    sources = load_sources(args.config)
    results = fetch_sources(
        sources=sources,
        raw_dir=args.raw_dir,
        clean_dir=args.clean_dir,
        metadata_dir=args.metadata_dir,
        review_path=args.review_path,
        timeout=args.timeout,
        min_clean_chars=args.min_clean_chars,
    )
    manifest = [asdict(result) for result in results]
    Path(args.manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"fetched_count": len(results), "manifest_path": args.manifest_path}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
