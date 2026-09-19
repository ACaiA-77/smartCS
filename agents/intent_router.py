"""
意图路由Agent — 用户意图识别与分类
负责分析用户输入，识别出具体的业务意图，为Supervisor提供路由依据。
支持多级意图分类：一级意图(咨询/投诉/办理) -> 二级意图(具体业务)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call


class IntentCategory(str, Enum):
    """一级意图分类"""
    CONVERSATION = "conversation"       # 无需检索或业务执行的自然对话
    CONSULTATION = "consultation"       # 咨询类
    COMPLAINT = "complaint"             # 投诉类
    TRANSACTION = "transaction"         # 交易/办理类
    ACCOUNT = "account"                 # 账户类
    COMPLIANCE = "compliance"           # 合规相关
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    """意图识别结果"""
    primary_intent: IntentCategory
    secondary_intent: str
    confidence: float
    entities: dict[str, str]
    suggested_agent: str


INTENT_SYSTEM_PROMPT = """你是一个专业的意图识别Agent，负责分析用户的客服消息。

请从以下维度分析用户意图：
1. 一级意图分类: conversation(自然对话), consultation(知识咨询), complaint(投诉), transaction(交易办理), account(账户), compliance(合规)
2. 二级意图: 具体的业务子类型
3. 置信度: 0.0-1.0
4. 关键实体: 提取订单号、产品名、金额等关键信息
5. 建议路由: conversation(自然对话), knowledge_rag(知识查询), order_query(订单查询), refund_handler(退款办理), ticket_handler(工单处理), compliance_checker(合规审查)

以JSON格式返回，示例：
{
    "primary_intent": "consultation",
    "secondary_intent": "product_inquiry",
    "confidence": 0.95,
    "entities": {"product": "理财产品A"},
    "suggested_agent": "knowledge_rag"
}

分流规则：
- 问候、自我介绍、身份/能力说明、致谢、告别、简单情绪交流，以及这些话题的上下文追问 → conversation；这些不需要知识库。
- 例如“你好，请问你是谁”“你能做什么”“谢谢你”“刚才你说的第二项能力是什么” → conversation。
- 查询产品事实、使用方法、故障排查、业务政策等需要文档依据的问题 → knowledge_rag，不要当作闲聊。
- 一句话同时包含寒暄和具体业务诉求时，优先具体诉求；例如“你好，帮我查订单” → order_query，“谢谢，帮我提交退款” → refund_handler。
- 联系近期对话理解省略和指代；不要因为上一轮查过订单，就把本轮问候、身份问题或致谢继续当作订单查询。
- 只有确认存在明确意图时才给高置信度；无法理解的问题给低置信度，让系统澄清，不要强行当作知识问题。

业务场景特殊规则：
- 涉及资金安全、账户异常、欺诈举报 → compliance_checker
- 涉及订单查询、物流查询、换一个订单或另一个订单 → order_query
- 涉及理赔、开户办理 → ticket_handler
- 退款问题只询问政策、条件、步骤、入口或“如何申请” → knowledge_rag
- 明确要求代为提交、办理或处理退款 → refund_handler
- 涉及产品咨询、利率查询、政策了解 → knowledge_rag
"""


ORDER_SIGNAL_RE = re.compile(
    r"订单|物流|快递|运单|发货|配送|收货|送达|签收|包裹",
)
ORDER_FOLLOW_UP_RE = re.compile(
    r"换一个|换一笔|另一个|另一笔|再看|再查|这个订单|这笔订单|这单|它|上一个|刚才",
)
ORDER_ID_RE = re.compile(r"(?<![A-Z0-9])ORD[-_][A-Z0-9-]+", re.IGNORECASE)
TICKET_ID_RE = re.compile(r"(?<![A-Z0-9])TK[-_][A-Z0-9-]+", re.IGNORECASE)
REFUND_DECISION_ORDER_RE = re.compile(r"ORD[-_][A-Z0-9-]+", re.IGNORECASE)
COMPLAINT_RE = re.compile(r"投诉|抱怨|不满|举报|维权|服务态度|乱收费")
COMPLIANCE_RE = re.compile(r"资金安全|账户被盗|被骗|诈骗|欺诈|可疑交易|转账异常")


class IntentRouterAgent:
    """意图路由Agent"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    @staticmethod
    def _confidence_at_least(result: dict[str, Any], floor: float) -> float:
        try:
            return max(float(result.get("confidence") or 0.0), floor)
        except (TypeError, ValueError):
            return floor

    @staticmethod
    def _format_chat_context(messages: list, max_turns: int = 6) -> str:
        """取最近若干轮对话文本，供意图分类参考（不含当前最后一条）。"""
        if len(messages) <= 1:
            return ""
        lines = []
        for m in messages[:-1][-max_turns:]:
            if isinstance(m, HumanMessage):
                lines.append(f"user: {m.content}")
            elif isinstance(m, AIMessage):
                lines.append(f"assistant: {m.content}")
        return "\n".join(lines)

    @staticmethod
    def _correct_refund_route(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
        """区分退款知识查询和明确的退款办理请求，避免副作用路由误判。"""
        if not re.search(r"退款|退货|退订|订阅|续费", user_message):
            return result

        knowledge_signal = re.search(
            r"怎么|如何|怎样|流程|步骤|政策|规则|条件|入口|在哪里|多久|资格|是否符合",
            user_message,
        )
        action_signal = re.search(
            r"帮我|请帮|替我|代我|请提交|提交(?:退款)?申请|发起(?:退款)?申请|办理退款|处理退款|我要退款|我要申请退款|帮我(?:取消|退订)",
            user_message,
        )

        if knowledge_signal and not action_signal:
            result.update(
                {
                    "primary_intent": IntentCategory.CONSULTATION.value,
                    "secondary_intent": "refund_policy",
                    "confidence": IntentRouterAgent._confidence_at_least(result, 0.85),
                    "suggested_agent": "knowledge_rag",
                }
            )
        elif action_signal:
            result.update(
                {
                    "primary_intent": IntentCategory.TRANSACTION.value,
                    "secondary_intent": "refund_request",
                    "confidence": IntentRouterAgent._confidence_at_least(result, 0.85),
                    "suggested_agent": "refund_handler",
                }
            )
        return result

    @staticmethod
    def _refund_decision(user_message: str) -> str | None:
        """Recognize short confirmation/cancellation replies without an LLM guess."""
        text = REFUND_DECISION_ORDER_RE.sub("", user_message or "")
        text = re.sub(r"[\s，,。.!！?？:：、]+", "", text)
        if text in {"确认", "确认退款", "是的确认", "是的确认退款", "提交", "提交退款", "提交退款申请", "同意", "同意退款", "好的确认", "好的确认退款", "好的提交退款"}:
            return "refund_confirm"
        if text in {"取消", "取消退款", "不用了", "先不退了", "不退了", "放弃退款"}:
            return "refund_cancel"
        return None

    @classmethod
    def _override_refund_decision(
        cls, user_message: str, pending_action: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(pending_action, dict):
            return None
        decision = cls._refund_decision(user_message)
        if decision is None:
            return None
        return {
            "primary_intent": IntentCategory.TRANSACTION.value,
            "secondary_intent": decision,
            "confidence": 1.0,
            "entities": cls._extract_entities(user_message),
            "suggested_agent": "refund_handler",
        }

    @staticmethod
    def _extract_entities(user_message: str) -> dict[str, str]:
        """从用户原文提取高价值实体，避免完全依赖模型是否正确返回 JSON。"""
        entities: dict[str, str] = {}
        order_match = ORDER_ID_RE.search(user_message.upper())
        if order_match:
            entities["order_id"] = order_match.group(0).replace("_", "-")

        ticket_match = TICKET_ID_RE.search(user_message.upper())
        if ticket_match:
            entities["ticket_id"] = ticket_match.group(0).replace("_", "-")

        return entities

    @staticmethod
    def _is_policy_question(user_message: str) -> bool:
        return bool(re.search(r"怎么|如何|怎样|流程|步骤|政策|规则|条件|入口|在哪里|多久|资格|是否符合", user_message))

    @staticmethod
    def _correct_complaint_route(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
        """投诉是有副作用的办理请求，不能被模型误判成普通知识问答。"""
        complaint_policy = re.search(r"投诉.{0,8}(?:政策|规则|流程|条件)|投诉状态", user_message)
        if COMPLAINT_RE.search(user_message) and not complaint_policy:
            result.update(
                {
                    "primary_intent": IntentCategory.COMPLAINT.value,
                    "secondary_intent": "complaint",
                    "confidence": IntentRouterAgent._confidence_at_least(result, 0.85),
                    "suggested_agent": "ticket_handler",
                }
            )
        return result

    @staticmethod
    def _correct_compliance_route(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
        if COMPLIANCE_RE.search(user_message):
            result.update(
                {
                    "primary_intent": IntentCategory.COMPLIANCE.value,
                    "secondary_intent": "financial_security",
                    "confidence": IntentRouterAgent._confidence_at_least(result, 0.9),
                    "suggested_agent": "compliance_checker",
                }
            )
        return result

    @staticmethod
    def _correct_order_route(
        user_message: str,
        result: dict[str, Any],
        last_intent: str | None = None,
        context_entities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用当前消息和会话上下文识别订单续问，避免误创建工单。"""
        context_entities = context_entities or {}
        has_order_signal = bool(ORDER_SIGNAL_RE.search(user_message))
        is_order_context = last_intent == "order_query" or bool(context_entities.get("order_id"))
        is_follow_up = bool(ORDER_FOLLOW_UP_RE.search(user_message))

        if not (has_order_signal or (is_order_context and is_follow_up)):
            return result

        # Explicit conversational intent can change topic despite stale order context.
        if not has_order_signal and result.get("suggested_agent") == "conversation":
            return result

        # 投诉、资金安全和退款办理优先级高于“订单”这个表面词。
        if COMPLAINT_RE.search(user_message) or COMPLIANCE_RE.search(user_message):
            return result
        if re.search(r"退款|退货|退订|订阅|续费", user_message) and not re.search(r"查询|状态|物流|发货", user_message):
            return result

        entities = result.get("entities")
        if not isinstance(entities, dict):
            entities = {}
        extracted_entities = IntentRouterAgent._extract_entities(user_message)
        # 当前轮没有订单号时，不采信模型从上下文中幻觉出来的旧订单号。
        if "order_id" in extracted_entities:
            entities["order_id"] = extracted_entities["order_id"]
        else:
            entities.pop("order_id", None)

        result.update(
            {
                "primary_intent": IntentCategory.CONSULTATION.value,
                "secondary_intent": "order_query",
                "confidence": IntentRouterAgent._confidence_at_least(result, 0.85),
                "suggested_agent": "order_query",
                "entities": entities,
            }
        )
        return result

    @trace_agent_call("intent_router")
    async def classify(
        self,
        user_message: str,
        chat_context: str = "",
        last_intent: str | None = None,
        context_entities: dict[str, Any] | None = None,
        pending_action: dict[str, Any] | None = None,
    ) -> IntentResult:
        """对用户消息进行意图分类"""
        if pending_action is None and isinstance(context_entities, dict):
            candidate = context_entities.get("pending_action")
            if isinstance(candidate, dict):
                pending_action = candidate
        decision_override = self._override_refund_decision(user_message, pending_action)
        if decision_override is not None:
            result = decision_override
        else:
            result = None
        human = f"用户消息: {user_message}"
        if chat_context.strip():
            human = f"近期对话:\n{chat_context}\n\n{human}"
        if last_intent:
            human = f"上一轮意图: {last_intent}\n\n{human}"
        messages = [
            SystemMessage(content=INTENT_SYSTEM_PROMPT),
            HumanMessage(content=human),
        ]

        if result is None:
            response = await self.llm.ainvoke(messages)

            try:
                content = response.content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("intent result is not an object")
            except (json.JSONDecodeError, TypeError, ValueError):
                result = {
                    "primary_intent": "unknown",
                    "secondary_intent": "unknown",
                    "confidence": 0.0,
                    "entities": {},
                    "suggested_agent": "knowledge_rag",
                }

        if not isinstance(result.get("entities"), dict):
            result["entities"] = {}
        result["entities"].update(self._extract_entities(user_message))
        result = self._correct_refund_route(user_message, result)
        result = self._correct_compliance_route(user_message, result)
        result = self._correct_complaint_route(user_message, result)
        result = self._correct_order_route(user_message, result, last_intent, context_entities)

        try:
            primary_intent = IntentCategory(result.get("primary_intent", "unknown"))
        except (TypeError, ValueError):
            primary_intent = IntentCategory.UNKNOWN
        try:
            confidence = min(max(float(result.get("confidence", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0

        return IntentResult(
            primary_intent=primary_intent,
            secondary_intent=result.get("secondary_intent", "unknown"),
            confidence=confidence,
            entities=result.get("entities", {}),
            suggested_agent=result.get("suggested_agent", "knowledge_rag"),
        )

    @trace_agent_call("intent_router_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        if not messages:
            return state

        last_message = messages[-1].content if messages else ""
        ctx = self._format_chat_context(messages)

        # 从 _session_context 读取上一轮意图
        context = state.get("sub_results", {}).get("_session_context", {})
        last_intent = context.get("last_intent")
        context_entities = context.get("accumulated_entities", {}) or {}
        pending_action = context.get("pending_action")

        intent_result = await self.classify(
            last_message,
            chat_context=ctx,
            last_intent=last_intent,
            context_entities=context_entities,
            pending_action=pending_action,
        )

        return {
            **state,
            "intent": intent_result.suggested_agent,
            "sub_results": {
                **state.get("sub_results", {}),
                "intent_router": {
                    "primary": intent_result.primary_intent.value,
                    "secondary": intent_result.secondary_intent,
                    "confidence": intent_result.confidence,
                    "entities": intent_result.entities,
                },
            },
        }
