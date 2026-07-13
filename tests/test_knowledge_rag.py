"""KnowledgeRAGAgent Apple taxonomy tests."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.knowledge_rag import KnowledgeRAGAgent
from memory.long_term import LongTermMemory
from tests.conftest import MockLLM


class RecordingLLM(MockLLM):
    """Records query-rewrite user input."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rewrite_inputs: list[str] = []

    async def ainvoke(self, messages):
        system, human = "", ""
        from langchain_core.messages import HumanMessage, SystemMessage

        for message in messages:
            if isinstance(message, SystemMessage):
                system += message.content
            elif isinstance(message, HumanMessage):
                human += message.content
        if "改写为更适合向量检索" in human:
            self.rewrite_inputs.append(human)
        return await super().ainvoke(messages)


@pytest.mark.asyncio
async def test_process_uses_apple_product_support_entity_in_rewrite():
    llm = RecordingLLM()
    agent = KnowledgeRAGAgent(llm, LongTermMemory())
    state = {
        "messages": [HumanMessage(content="怎么更换电池？")],
        "sub_results": {
            "intent_router": {
                "secondary": "product_support",
                "entities": {"product": "iPhone"},
            }
        },
    }

    await agent.process(state)

    assert llm.rewrite_inputs
    assert "iPhone" in llm.rewrite_inputs[0]
