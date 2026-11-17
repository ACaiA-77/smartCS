"""Shared pytest fixtures for SmartCS tests."""

from __future__ import annotations

import os

import pytest

from memory.long_term import LongTermMemory

# Keep unit and integration tests deterministic; the service loads production
# embedding settings from .env only when it is started outside pytest.
os.environ.setdefault("EMBEDDING_BACKEND", "hash")


@pytest.fixture
def seeded_long_term_memory() -> LongTermMemory:
    """Default knowledge base fixture for RAG integration tests."""
    mem = LongTermMemory()
    mem.add_document(
        "我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。"
        "注意：理财非存款，产品有风险，投资须谨慎。",
        metadata={"source": "product_a.md", "category": "理财产品"},
    )
    mem.add_document(
        "订单发货后一般3-5个工作日送达，偏远地区5-7个工作日。支持7天无理由退货（定制商品除外）。",
        metadata={"source": "shipping_policy.md", "category": "物流售后"},
    )
    mem.add_document(
        "开户需提供本人有效身份证件及同名银行卡，风险测评结果有效期为2年，过期需重新测评。",
        metadata={"source": "account_opening.md", "category": "账户开户"},
    )
    return mem
