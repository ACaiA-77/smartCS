"""Deterministic, network-free doubles used only by the eval harness."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from memory.short_term import ShortTermMemory


def _message_text(messages: list[BaseMessage]) -> tuple[str, str]:
    system: list[str] = []
    human: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            system.append(str(message.content))
        elif isinstance(message, HumanMessage):
            human.append(str(message.content))
    return "\n".join(system), "\n".join(human)


class DeterministicEvalLLM:
    """Return local responses selected by prompt role and optional overrides."""

    def __init__(self, overrides: dict[str, Any] | None = None):
        self.overrides = dict(overrides or {})
        self.call_count = 0
        self.call_log: list[tuple[str, str]] = []

    def set_override(self, name: str, value: Any) -> None:
        self.overrides[name] = value

    def _resolve(self, name: str, default: Any, system: str, human: str) -> Any:
        value = self.overrides.get(name, default)
        if callable(value):
            value = value(system, human)
        return value

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.call_count += 1
        system, human = _message_text(messages)
        self.call_log.append((system, human))

        if "意图识别Agent" in system:
            result = self._resolve("intent_router", self._default_intent(human), system, human)
        elif "工单处理Agent" in system:
            result = self._resolve("ticket_handler", self._default_ticket(human), system, human)
        elif "文档相关性排序" in system:
            result = self._resolve("rag_rank", "0", system, human)
        elif "知识库问答Agent" in system:
            result = self._resolve("rag_answer", "知识库暂未提供更多信息。", system, human)
        elif "合规审查Agent" in system:
            result = self._resolve(
                "compliance",
                {"passed": True, "risk_level": "low", "violations": [], "suggestions": []},
                system,
                human,
            )
        elif "改写为更适合向量检索" in human:
            result = self._resolve("rag_rewrite", human, system, human)
        else:
            result = self._resolve("default", "ok", system, human)

        if isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False)
        return AIMessage(content=str(result))

    @staticmethod
    def _default_intent(human: str) -> dict[str, Any]:
        if "确认退款" in human or "取消退款" in human:
            secondary = "refund_confirm" if "确认" in human else "refund_cancel"
            return {
                "primary_intent": "transaction",
                "secondary_intent": secondary,
                "confidence": 1.0,
                "entities": {},
                "suggested_agent": "refund_handler",
            }
        if "投诉" in human:
            return {
                "primary_intent": "complaint",
                "secondary_intent": "complaint",
                "confidence": 0.95,
                "entities": {},
                "suggested_agent": "ticket_handler",
            }
        if "退款" in human:
            return {
                "primary_intent": "transaction",
                "secondary_intent": "refund_request",
                "confidence": 0.95,
                "entities": {},
                "suggested_agent": "refund_handler",
            }
        if "订单" in human:
            return {
                "primary_intent": "consultation",
                "secondary_intent": "order_query",
                "confidence": 0.95,
                "entities": {},
                "suggested_agent": "order_query",
            }
        return {
            "primary_intent": "consultation",
            "secondary_intent": "policy_inquiry",
            "confidence": 0.95,
            "entities": {},
            "suggested_agent": "knowledge_rag",
        }

    @staticmethod
    def _default_ticket(human: str) -> dict[str, Any]:
        if "查询工单" in human or "查询" in human and "工单" in human:
            return {"action": "query", "ticket_type": "general", "priority": "low"}
        return {
            "action": "create",
            "ticket_type": "complaint" if "投诉" in human else "general",
            "priority": "medium",
            "summary": "服务问题",
            "details": "用户请求客服处理服务问题",
        }


class OfflineShortTermMemory(ShortTermMemory):
    """Short-term store double whose Redis path is impossible to enter."""

    async def _get_redis(self):
        return None
