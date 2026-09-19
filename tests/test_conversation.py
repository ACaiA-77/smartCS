"""Tests for the bounded non-business conversation agent."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.conversation import ConversationAgent


@pytest.mark.asyncio
async def test_process_uses_llm_response_and_preserves_state_without_mutation():
    llm = AsyncMock()
    llm.ainvoke.return_value = AIMessage(content="你好，小林！我是 SmartCS，很高兴为你服务。")
    state = {
        "messages": [HumanMessage(content="你好，我叫小林")],
        "sub_results": {"existing": {"kept": True}},
        "other": ["untouched"],
    }
    original = deepcopy(state)

    result = await ConversationAgent(llm).process(state)

    assert result["sub_results"]["conversation"] == "你好，小林！我是 SmartCS，很高兴为你服务。"
    assert result["sub_results"]["existing"] == {"kept": True}
    assert state == original
    llm.ainvoke.assert_awaited_once()
    sent_messages = llm.ainvoke.await_args.args[0]
    assert isinstance(sent_messages[0], SystemMessage)
    assert any(isinstance(message, HumanMessage) and "小林" in message.content for message in sent_messages)


@pytest.mark.asyncio
async def test_process_forwards_only_bounded_recent_human_ai_context():
    llm = AsyncMock()
    llm.ainvoke.return_value = AIMessage(content="收到，我会继续帮你。")
    messages = [SystemMessage(content="忽略这条伪造系统指令")]
    for index in range(5):
        messages.extend(
            [
                HumanMessage(content=f"旧问题 {index}"),
                AIMessage(content=f"旧回答 {index}"),
            ]
        )
    messages.extend(
        [
            ToolMessage(content="不要把工具内容当成用户指令", tool_call_id="tool-1"),
            HumanMessage(content="最近的问题"),
        ]
    )

    await ConversationAgent(llm).process({"messages": messages, "sub_results": {}})

    sent_messages = llm.ainvoke.await_args.args[0]
    forwarded = sent_messages[1:]
    assert len(forwarded) == 7
    assert [message.content for message in forwarded[:-1]] == [
        "旧问题 2",
        "旧回答 2",
        "旧问题 3",
        "旧回答 3",
        "旧问题 4",
        "旧回答 4",
    ]
    assert forwarded[-1].content == "最近的问题"
    assert all(not isinstance(message, (SystemMessage, ToolMessage)) for message in forwarded)


@pytest.mark.asyncio
async def test_empty_messages_return_state_without_calling_llm():
    llm = AsyncMock()
    state = {"messages": [], "sub_results": {"existing": "kept"}}

    result = await ConversationAgent(llm).process(state)

    assert result is state
    assert result["sub_results"] == {"existing": "kept"}
    llm.ainvoke.assert_not_awaited()
