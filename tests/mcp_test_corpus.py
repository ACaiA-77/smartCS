"""The isolated corpus shared by the MCP gateway tests and their launcher.

Kept import-free (data + one factory over `memory.long_term`) so both the test
process — where the repository's own `mcp/` package is importable — and the
gateway process — where it must not be — can load the exact same documents.
"""

from __future__ import annotations

DOCUMENTS = [
    ("退款政策：用户在购买后 7 天内可申请无理由退款，退款将在 3-5 个工作日内原路退回。", "refund_policy.md"),
    ("工单受理后 24 小时内首次响应，48 小时内给出处理结论。", "ticket_sla.md"),
    ("订单发货后可在物流页面查询轨迹，签收后 7 天内支持退换。", "shipping.md"),
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
