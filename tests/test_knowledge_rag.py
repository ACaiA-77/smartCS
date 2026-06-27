"""KnowledgeRAGAgent 二级意图改写测试。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.knowledge_rag import KnowledgeRAGAgent
from memory.long_term import LongTermMemory
from tests.conftest import MockLLM


class RecordingLLM(MockLLM):
    """记录 Query 改写时的 human 输入。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rewrite_inputs: list[str] = []

    async def ainvoke(self, messages):
        system, human = "", ""
        from langchain_core.messages import HumanMessage, SystemMessage
        for m in messages:
            if isinstance(m, SystemMessage):
                system += m.content
            elif isinstance(m, HumanMessage):
                human += m.content
        if "改写为更适合向量检索" in human:
            self.rewrite_inputs.append(human)
        return await super().ainvoke(messages)


@pytest.mark.asyncio
async def test_process_uses_product_entity_in_rewrite():
    llm = RecordingLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_inquiry",
                "confidence": 0.95,
                "entities": {"product": "理财产品A"},
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    agent = KnowledgeRAGAgent(llm, LongTermMemory())
    state = {
        "messages": [HumanMessage(content="收益怎么样？")],
        "sub_results": {
            "intent_router": {
                "secondary": "product_inquiry",
                "entities": {"product": "理财产品A"},
            }
        },
    }
    await agent.process(state)

    assert llm.rewrite_inputs
    assert "理财产品A" in llm.rewrite_inputs[0]
