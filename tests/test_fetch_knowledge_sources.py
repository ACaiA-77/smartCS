from __future__ import annotations

import json

import httpx
import scripts.fetch_knowledge_sources as fetcher

from scripts.fetch_knowledge_sources import (
    CleanedContent,
    KnowledgeSource,
    clean_html_to_markdown,
    fetch_sources,
    load_sources,
)


def test_load_sources_parses_simple_urls_yml(tmp_path):
    config = tmp_path / "urls.yml"
    config.write_text(
        """
sources:
  - id: apple_test
    title: Apple 测试页面
    url: https://example.com/apple
    brand: Apple 中国
    doc_type: 产品FAQ
    language: zh-CN
    output: apple_test.md
""",
        encoding="utf-8",
    )

    sources = load_sources(config)

    assert len(sources) == 1
    assert sources[0].id == "apple_test"
    assert sources[0].url == "https://example.com/apple"
    assert sources[0].output == "apple_test.md"


def test_clean_html_to_markdown_removes_scripts_and_keeps_structure(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_extract_with_trafilatura",
        lambda html, url="": CleanedContent("", "", "trafilatura_unavailable"),
    )
    monkeypatch.setattr(
        fetcher,
        "_extract_with_readability_html2text",
        lambda html, url="": CleanedContent("", "", "readability_unavailable"),
    )
    source = KnowledgeSource(
        id="apple_refund",
        title="Apple 退货政策",
        url="https://example.com/refund",
        brand="Apple 中国",
        doc_type="退款政策",
    )
    html = """
<html>
  <head><title>网页标题</title><script>ignore_me()</script></head>
  <body>
    <nav>导航 首页 支持</nav>
    <main>
      <h1>退货与退款</h1>
      <p>符合条件的商品，应在商品交付之日起 14 个自然日内申请退货。</p>
      <ul><li>退回产品必须带有原包装。</li></ul>
    </main>
    <script>bad text</script>
  </body>
</html>
"""

    markdown, metadata = clean_html_to_markdown(html, source, "2026-07-06T00:00:00+00:00")

    assert "script" not in markdown.lower()
    assert "bad text" not in markdown
    assert "导航 首页 支持" not in markdown
    assert "# Apple 退货政策" in markdown
    assert "14 个自然日" in markdown
    assert "- 退回产品必须带有原包装。" in markdown
    assert metadata["source_url"] == "https://example.com/refund"
    assert metadata["clean_chars"] > 0
    assert metadata["cleaner"] == "builtin_htmlparser"


def test_clean_html_to_markdown_prefers_trafilatura(monkeypatch):
    source = KnowledgeSource(
        id="apple_refund",
        title="Apple 退货政策",
        url="https://example.com/refund",
        brand="Apple 中国",
        doc_type="退款政策",
    )
    monkeypatch.setattr(
        fetcher,
        "_extract_with_trafilatura",
        lambda html, url="": CleanedContent("## 正文\n\n14 天内申请退货。", "网页标题", "trafilatura"),
    )

    markdown, metadata = clean_html_to_markdown("<html></html>", source, "2026-07-06T00:00:00+00:00")

    assert "14 天内申请退货" in markdown
    assert metadata["page_title"] == "网页标题"
    assert metadata["cleaner"] == "trafilatura"


def test_fetch_sources_writes_raw_clean_metadata_and_review(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="""
<html><head><title>Apple 支持</title></head>
<body><main><h1>Apple 支持</h1><p>用户可以查看保修状态。</p></main></body></html>
""",
        )

    transport = httpx.MockTransport(handler)
    source = KnowledgeSource(
        id="apple_support",
        title="Apple 支持",
        url="https://example.com/support",
        output="apple_support.md",
    )

    original_client = httpx.Client

    class MockedClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.Client = MockedClient
    try:
        results = fetch_sources(
            [source],
            raw_dir=tmp_path / "raw",
            clean_dir=tmp_path / "clean",
            metadata_dir=tmp_path / "metadata",
            review_path=tmp_path / "review.md",
            min_clean_chars=1,
        )
    finally:
        httpx.Client = original_client

    assert len(results) == 1
    assert (tmp_path / "raw" / "apple_support.html").exists()
    clean_path = tmp_path / "clean" / "apple_support.md"
    assert "查看保修状态" in clean_path.read_text(encoding="utf-8")
    metadata = json.loads((tmp_path / "metadata" / "apple_support.json").read_text(encoding="utf-8"))
    assert metadata["source_id"] == "apple_support"
    assert "apple_support" in (tmp_path / "review.md").read_text(encoding="utf-8")
