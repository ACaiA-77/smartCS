"""The isolated corpus shared by the MCP gateway tests and their launcher.

Kept import-free (data + one factory over `memory.long_term`) so both the test
process — where the repository's own `mcp/` package is importable — and the
gateway process — where it must not be — can load the exact same documents.
"""

from __future__ import annotations

DOCUMENTS = [
    ("上下文工程原则：每轮注入确定性权威状态快照，不依赖模型历史转录。", "context_engineering.md"),
    ("双段式外部操作：先准备冻结快照，待用户明确确认后再执行发送。", "two_phase_gate.md"),
    ("Obsidian LLM Wiki：raw 区域保持只读归档，wiki 区域由编译器维护。", "wiki_architecture.md"),
]


def build_memory():
    from memory.long_term import LongTermMemory

    memory = LongTermMemory(embedding_dim=64)
    for content, source in DOCUMENTS:
        memory.add_document(content, source)
    return memory


def build_retriever():
    # use_env=False: the isolated index is the corpus, not the production RAG env.
    return build_memory().get_retriever(use_env=False)
