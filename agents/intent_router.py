"""
意图路由Agent — Apple 售后意图识别与分类（Task 2 审查修复）。

基于 Task 1 的 Apple 售后 taxonomy（PrimaryIntent / SecondaryIntent / AgentTarget / ReasonCode）
和 build_intent_decision() 服务端确定性重算。

解析流程：首次 LLM 调用 → JSON 提取 → build_intent_decision 校验
→ 失败时一次格式修复重试 → 再失败规则降级 _fallback_decision。

审查修复：
- 消除实例级可变状态 _last_parse_mode，改用 dataclass ClassifyOutcome
- process() 同时写顶层 state["needs_clarification"] 和 sub_results
- 格式修复 prompt 只携带坏输出与 schema，不含原始用户消息
- 候选置信度差值阈值（candidate_margin）判定澄清
- confidence_threshold / candidate_margin / enable_format_repair 可注入

规则降级顺序：安全 > 订单/工单查询 > 明确动作 > 普通咨询 > 未知（低置信度）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from agents.intent_models import (
    AgentTarget,
    IntentCandidate,
    IntentDecision,
    IntentEntities,
    PrimaryIntent,
    ReasonCode,
    SecondaryIntent,
    build_intent_decision,
)
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Apple 售后意图识别 System Prompt
# ═══════════════════════════════════════════════════════════════════

INTENT_SYSTEM_PROMPT = """你是一个专业的 Apple 售后意图识别 Agent。请分析用户消息，仅输出一个 JSON 对象（不要 Markdown 代码块、不要额外解释）。

JSON 格式（所有字段必填）：
{
    "primary_intent": "<security | action | query | consultation | complaint | unknown>",
    "secondary_intent": "<精确二级分类>",
    "confidence": <0.0-1.0 浮点数>,
    "entities": { },
    "candidates": [ ],
    "reason_code": "<explicit_policy_question | explicit_action_request | explicit_status_query | security_risk_detected | context_follow_up | ambiguous_request>",
    "suggested_agent": "<knowledge_rag | ticket_handler | compliance_checker>"
}

二级意图完整列表（必须从以下枚举中取值）：
- knowledge_rag 组：product_support, repair_warranty_policy, return_refund_policy, subscription_policy, account_guidance, sales_policy
- ticket_handler 组：order_query, refund_request, repair_request, subscription_cancel_request, complaint, human_escalation
- compliance_checker 组：account_security, fraud_report, sensitive_data, prohibited_request

实体字段白名单（仅以下字段，值不超过128字符）：
order_id (格式 ORD-…), ticket_id (格式 TK-…), product, device_model, subscription, account_issue, region

分级规则（优先级从高到低）：
1. 涉及账户被盗、异常登录、验证码泄露、欺诈举报 → primary=security, agent=compliance_checker
2. 涉及订单编号(ORD-)/工单编号(TK-)查询 → primary=query, agent=ticket_handler
3. 涉及退款申请、维修申请、取消订阅、投诉、转人工 → primary=action, agent=ticket_handler
4. 涉及产品使用、政策咨询、保修条款、AppleCare → primary=consultation, agent=knowledge_rag
5. 无法明确判断 → primary=unknown, confidence < 0.5

candidates 为备选意图列表（0-3项），每项含 primary_intent, secondary_intent, confidence。无备选时传空数组 []。

只输出 JSON。"""

# ═══════════════════════════════════════════════════════════════════
# 格式修复重试 Prompt
# ═══════════════════════════════════════════════════════════════════

FORMAT_REPAIR_PROMPT = """你上一次输出的 JSON 格式不正确，无法解析。请严格按照以下规范重新输出 Apple 售后意图识别 JSON。

必须包含且仅包含以下字段：primary_intent, secondary_intent, confidence, entities, candidates, reason_code, suggested_agent。

二级意图合法值：
knowledge_rag: product_support | repair_warranty_policy | return_refund_policy | subscription_policy | account_guidance | sales_policy
ticket_handler: order_query | refund_request | repair_request | subscription_cancel_request | complaint | human_escalation
compliance_checker: account_security | fraud_report | sensitive_data | prohibited_request

只输出纯 JSON 对象，不要有任何前缀、后缀或 Markdown 代码块标记。

以下是上一次的输出内容（仅用于格式修复，不要重新做业务判断）：
{previous_output}"""

# ═══════════════════════════════════════════════════════════════════
# JSON 提取正则
# ═══════════════════════════════════════════════════════════════════

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> str | None:
    """从 LLM 输出中尝试提取 JSON 对象。

    优先匹配 Markdown 代码块，其次匹配裸 JSON 对象。
    """
    if not text or not text.strip():
        return None
    # 尝试 Markdown 代码块
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # 尝试裸 JSON 对象
    m = _JSON_OBJECT_RE.search(text)
    if m:
        return m.group(0).strip()
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# 规则降级：安全 > 订单/工单 > 动作 > 咨询 > 未知
# ═══════════════════════════════════════════════════════════════════

# 安全关键词
_SECURITY_KEYWORDS: list[str] = [
    "被盗", "盗号", "黑客", "验证码", "异常登录", "远程登录",
    "异地登录", "密码泄露", "账户安全", "欺诈", "被骗", "诈骗",
    "盗刷", "账号被盗", "异地", "不是我操作", "有人登录",
]

# 订单/工单 ID 模式
_ORDER_ID_RE = re.compile(r"ORD-[A-Za-z0-9-]{1,59}")
_TICKET_ID_RE = re.compile(r"TK-[A-Za-z0-9-]{1,59}")

# 明确动作关键词
_ACTION_KEYWORDS: list[str] = [
    "退款", "退货", "维修", "修理", "换货", "取消订阅",
    "取消AppleCare", "投诉", "转人工", "申请退款",
]

# 动作→二级意图映射
_ACTION_TO_SECONDARY: dict[str, SecondaryIntent] = {
    "退款": SecondaryIntent.REFUND_REQUEST,
    "退货": SecondaryIntent.REFUND_REQUEST,
    "申请退款": SecondaryIntent.REFUND_REQUEST,
    "维修": SecondaryIntent.REPAIR_REQUEST,
    "修理": SecondaryIntent.REPAIR_REQUEST,
    "换货": SecondaryIntent.REPAIR_REQUEST,
    "取消订阅": SecondaryIntent.SUBSCRIPTION_CANCEL_REQUEST,
    "取消AppleCare": SecondaryIntent.SUBSCRIPTION_CANCEL_REQUEST,
    "投诉": SecondaryIntent.COMPLAINT,
    "转人工": SecondaryIntent.HUMAN_ESCALATION,
}

# 咨询关键词
_CONSULT_KEYWORDS: list[str] = [
    "怎么", "如何", "什么", "功能", "使用", "设置", "配置",
    "保修", "政策", "价格", "AppleCare", "电池", "屏幕",
    "取消", "退订", "续费", "订阅",
]

_REPAIR_FOLLOW_UP_KEYWORDS = ("预约维修", "预约", "报修", "维修", "修理", "送修")
_CONTEXT_ENTITY_KEYS = {
    "product", "device_model", "subscription", "account_issue", "region", "order_id", "ticket_id",
}
_CONTEXT_DEVICE_RE = re.compile(
    r"(?i)\b(iPhone(?:\s+(?:\d{1,2}|SE)(?:\s+(?:Pro|Plus|Max))?)?|"
    r"iPad(?:\s+[A-Za-z0-9 ]+)?|MacBook(?:\s+[A-Za-z0-9 ]+)?|"
    r"Apple Watch(?:\s+[A-Za-z0-9 ]+)?)\b"
)


def _contextual_repair_follow_up_decision(
    user_message: str,
    last_intent: str | None,
    accumulated_entities: dict[str, str] | None,
    chat_context: str,
) -> IntentDecision | None:
    """Resolve an explicit repair booking that continues a prior device conversation.

    This is intentionally evaluated before the LLM.  A customer saying “那我想预约维修”
    after a device-policy turn has supplied a business action, not an ambiguous category.
    """
    if last_intent not in {"knowledge_rag", "ticket_handler"}:
        return None
    if any(keyword in user_message for keyword in _SECURITY_KEYWORDS):
        return None
    if not any(keyword in user_message for keyword in _REPAIR_FOLLOW_UP_KEYWORDS):
        return None

    entities = {
        key: value
        for key, value in (accumulated_entities or {}).items()
        if key in _CONTEXT_ENTITY_KEYS and value
    }
    if not entities.get("product") and not entities.get("device_model"):
        match = _CONTEXT_DEVICE_RE.search(chat_context)
        if match:
            entities["product"] = match.group(1).strip()
    if not entities.get("product") and not entities.get("device_model"):
        return None

    return build_intent_decision(
        {
            "primary_intent": "action",
            "secondary_intent": "repair_request",
            "confidence": 0.94,
            "entities": entities,
            "candidates": [],
            "reason_code": "context_follow_up",
            "suggested_agent": "ticket_handler",
        }
    )


def _fallback_decision(user_message: str) -> IntentDecision:
    """规则降级：基于关键词和模式的确定性意图决策。

    优先级：安全 > 订单/工单查询 > 明确动作 > 普通咨询 > 未知（低置信度）。
    """
    # ── 1. 安全检测 ──
    for kw in _SECURITY_KEYWORDS:
        if kw in user_message:
            return build_intent_decision(
                {
                    "primary_intent": "security",
                    "secondary_intent": "account_security",
                    "confidence": 0.85,
                    "entities": {},
                    "candidates": [],
                    "reason_code": "parser_fallback",
                    "suggested_agent": "compliance_checker",
                }
            )

    # ── 2. 订单/工单 ID 查询 ──
    order_match = _ORDER_ID_RE.search(user_message)
    ticket_match = _TICKET_ID_RE.search(user_message)
    if order_match or ticket_match:
        entities: dict[str, Any] = {}
        if order_match:
            entities["order_id"] = order_match.group()
        if ticket_match:
            entities["ticket_id"] = ticket_match.group()
        return build_intent_decision(
            {
                "primary_intent": "query",
                "secondary_intent": "order_query",
                "confidence": 0.80,
                "entities": entities,
                "candidates": [],
                "reason_code": "parser_fallback",
                "suggested_agent": "ticket_handler",
            }
        )

    # ── 3. AppleCare / 订阅政策问句（先于动作词） ──
    is_question = any(marker in user_message for marker in ("吗", "？", "?", "是否", "可以", "能否", "怎么"))
    if is_question and any(token in user_message for token in ("AppleCare", "订阅", "续费", "取消")):
        return build_intent_decision(
            {
                "primary_intent": "consultation",
                "secondary_intent": "subscription_policy",
                "confidence": 0.72,
                "entities": {"subscription": "AppleCare"} if "AppleCare" in user_message else {},
                "candidates": [],
                "reason_code": "parser_fallback",
                "suggested_agent": "knowledge_rag",
            }
        )

    # Explicit cancellation requests may include whitespace or words between “取消” and “AppleCare”.
    if "取消" in user_message and any(token in user_message for token in ("AppleCare", "订阅", "iCloud")):
        return build_intent_decision(
            {
                "primary_intent": "action",
                "secondary_intent": "subscription_cancel_request",
                "confidence": 0.80,
                "entities": {"subscription": "AppleCare"} if "AppleCare" in user_message else {},
                "candidates": [],
                "reason_code": "parser_fallback",
                "suggested_agent": "ticket_handler",
            }
        )

    # ── 4. 明确 Apple 售后动作 ──
    for kw, secondary in _ACTION_TO_SECONDARY.items():
        if kw in user_message:
            return build_intent_decision(
                {
                    "primary_intent": "action",
                    "secondary_intent": secondary.value,
                    "confidence": 0.80,
                    "entities": {},
                    "candidates": [],
                    "reason_code": "parser_fallback",
                    "suggested_agent": "ticket_handler",
                }
            )

    # ── 4. 普通咨询 ──
    for kw in _CONSULT_KEYWORDS:
        if kw in user_message:
            return build_intent_decision(
                {
                    "primary_intent": "consultation",
                    "secondary_intent": "product_support",
                    "confidence": 0.65,
                    "entities": {},
                    "candidates": [],
                    "reason_code": "parser_fallback",
                    "suggested_agent": "knowledge_rag",
                }
            )

    # ── 5. 未知 / 无法判断 → 低置信度回退 ──
    return build_intent_decision(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.30,
            "entities": {},
            "candidates": [],
            "reason_code": "parser_fallback",
            "suggested_agent": "knowledge_rag",
        }
    )


# ═══════════════════════════════════════════════════════════════════
# 不可变内部结果
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ClassifyOutcome:
    """不可变内部结果：封装 classify 的完整输出。

    公共 classify() 仍返回 IntentDecision；
    process() 使用 ClassifyOutcome 一次拿到 decision + metadata。
    """

    decision: IntentDecision
    parse_mode: str
    needs_clarification: bool


# ═══════════════════════════════════════════════════════════════════
# 纯函数：澄清判定
# ═══════════════════════════════════════════════════════════════════


def _compute_needs_clarification(
    decision: IntentDecision,
    confidence_threshold: float,
    candidate_margin: float,
) -> bool:
    """纯函数：根据置信度阈值和候选差值判定是否需要澄清。

    触发条件：
    - 最终置信度 < confidence_threshold；或
    - 存在候选且主意图与最高候选置信度差 < candidate_margin。
    """
    if decision.confidence < confidence_threshold:
        return True
    if decision.candidates:
        top_candidate_conf = decision.candidates[0].confidence
        if abs(decision.confidence - top_candidate_conf) < candidate_margin:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
# IntentRouterAgent
# ═══════════════════════════════════════════════════════════════════


class IntentRouterAgent:
    """Apple 售后意图路由 Agent。

    通过 LLM 结构化输出 + build_intent_decision 服务端重算实现意图分类。
    解析失败时最多一次格式修复，二次失败触发规则降级。

    构造参数（待 Task 4 接 AppSettings）：
        confidence_threshold: 澄清置信度阈值，默认 0.70
        candidate_margin: 前两项意图置信度差阈值，默认 0.15
        enable_format_repair: 是否启用格式修复重试，默认 True
    """

    def __init__(
        self,
        llm: ChatOpenAI,
        confidence_threshold: float = 0.70,
        candidate_margin: float = 0.15,
        enable_format_repair: bool = True,
        context_turns: int = 3,
        prompt_version: str = "apple-support-v1",
    ):
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not 0.0 <= candidate_margin <= 1.0:
            raise ValueError("candidate_margin must be between 0 and 1")
        if context_turns < 1:
            raise ValueError("context_turns must be positive")
        self.llm = llm
        self.confidence_threshold = confidence_threshold
        self.candidate_margin = candidate_margin
        self.enable_format_repair = enable_format_repair
        self.context_turns = context_turns
        self.prompt_version = prompt_version


    # ── 内部：JSON 解析 ──

    @staticmethod
    def _try_parse_json(raw_text: str) -> dict[str, Any] | None:
        """从 LLM 输出中提取并解析 JSON dict。失败返回 None。"""
        extracted = _extract_json(raw_text)
        if extracted is None:
            return None
        try:
            parsed = json.loads(extracted)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    @staticmethod
    def _try_build_decision(raw_dict: dict[str, Any]) -> IntentDecision | None:
        """尝试通过 build_intent_decision 构建 IntentDecision。

        捕获 ValidationError（包括 Pydantic 校验和模型级 validator 错误）。
        """
        try:
            return build_intent_decision(raw_dict)
        except ValidationError:
            return None
        except (ValueError, TypeError, KeyError):
            return None

    # ── 上下文格式化 ──

    @staticmethod
    def _format_chat_context(messages: list, max_turns: int = 6) -> str:
        """取最近若干轮对话文本，供意图分类参考（不含当前最后一条）。"""
        if len(messages) <= 1:
            return ""
        lines: list[str] = []
        for m in messages[:-1][-max_turns:]:
            if isinstance(m, HumanMessage):
                lines.append(f"user: {m.content}")
            elif isinstance(m, AIMessage):
                lines.append(f"assistant: {m.content}")
        return "\n".join(lines)

    # ── classify_internal：返回完整 ClassifyOutcome ──

    @trace_agent_call("intent_router")
    async def classify_internal(
        self,
        user_message: str,
        chat_context: str = "",
        last_intent: str | None = None,
        accumulated_entities: dict[str, str] | None = None,
    ) -> ClassifyOutcome:
        """对用户消息进行意图分类，返回不可变 ClassifyOutcome。

        流程：首次 LLM 调用 → JSON 提取 → build_intent_decision 校验
        → 失败时一次格式修复重试 → 再失败规则降级 _fallback_decision。
        """
        contextual_decision = _contextual_repair_follow_up_decision(
            user_message,
            last_intent=last_intent,
            accumulated_entities=accumulated_entities,
            chat_context=chat_context,
        )
        if contextual_decision is not None:
            return ClassifyOutcome(
                decision=contextual_decision,
                parse_mode="context_follow_up",
                needs_clarification=False,
            )

        # ── 构建消息 ──
        human_lines = [f"用户消息: {user_message}"]
        if chat_context.strip():
            human_lines.insert(0, f"近期对话:\n{chat_context}")
        if last_intent:
            human_lines.insert(0, f"上一轮意图: {last_intent}")
        if accumulated_entities:
            entity_str = ", ".join(
                f"{k}={v}" for k, v in accumulated_entities.items() if v
            )
            if entity_str:
                human_lines.insert(0, f"累积实体: {entity_str}")

        human_text = "\n\n".join(human_lines)
        base_messages: list = [
            SystemMessage(content=INTENT_SYSTEM_PROMPT),
            HumanMessage(content=human_text),
        ]

        # ── 首次调用 ──
        response = await self.llm.ainvoke(base_messages)
        first_raw_content = response.content  # 捕获原始输出，供格式修复使用
        parsed = self._try_parse_json(first_raw_content)
        if parsed is not None:
            decision = self._try_build_decision(parsed)
            if decision is not None:
                return ClassifyOutcome(
                    decision=decision,
                    parse_mode="direct_parse",
                    needs_clarification=_compute_needs_clarification(
                        decision, self.confidence_threshold, self.candidate_margin
                    ),
                )

        # ── 格式修复重试（最多一次）──
        if self.enable_format_repair:
            repair_prompt = FORMAT_REPAIR_PROMPT.format(
                previous_output=first_raw_content
            )
            repair_messages: list = [
                SystemMessage(content=repair_prompt),
                HumanMessage(
                    content="你上次返回的内容无法解析。请严格按规范输出 JSON，不要包含任何与原始业务判断无关的内容。"
                ),
            ]
            repair_response = await self.llm.ainvoke(repair_messages)
            parsed = self._try_parse_json(repair_response.content)
            if parsed is not None:
                decision = self._try_build_decision(parsed)
                if decision is not None:
                    return ClassifyOutcome(
                        decision=decision,
                        parse_mode="format_repair",
                        needs_clarification=_compute_needs_clarification(
                            decision, self.confidence_threshold, self.candidate_margin
                        ),
                    )

        # ── 规则降级 ──
        logger.warning(
            "IntentRouter falling back to rule-based decision for: %s",
            user_message[:100],
        )
        decision = _fallback_decision(user_message)
        return ClassifyOutcome(
            decision=decision,
            parse_mode="rule_fallback",
            needs_clarification=_compute_needs_clarification(
                decision, self.confidence_threshold, self.candidate_margin
            ),
        )

    # ── classify：公共接口（保持向后兼容） ──

    async def classify(
        self,
        user_message: str,
        chat_context: str = "",
        last_intent: str | None = None,
        accumulated_entities: dict[str, str] | None = None,
    ) -> IntentDecision:
        """对用户消息进行意图分类，返回 IntentDecision。

        委托给 classify_internal()，丢弃元数据。
        """
        outcome = await self.classify_internal(
            user_message,
            chat_context=chat_context,
            last_intent=last_intent,
            accumulated_entities=accumulated_entities,
        )
        return outcome.decision

    # ── process：Graph 节点 ──

    @trace_agent_call("intent_router_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为 Graph 节点处理状态，将意图决策写入 state。

        process() 通过 classify_internal() 一次拿到 decision + metadata，
        同时写入顶层 state["needs_clarification"] 和 sub_results 中，
        避免 Supervisor 二次重算。
        """
        messages = state.get("messages", [])
        if not messages:
            return state

        last_message = messages[-1].content if messages else ""
        ctx = self._format_chat_context(messages, max_turns=self.context_turns)

        # 从 _wm_context 读取上一轮意图和累积实体
        wm = state.get("sub_results", {}).get("_wm_context", {})
        last_intent = wm.get("last_intent")
        accumulated_entities = wm.get("accumulated_entities", {})

        outcome = await self.classify_internal(
            last_message,
            chat_context=ctx,
            last_intent=last_intent,
            accumulated_entities=accumulated_entities,
        )

        decision = outcome.decision

        # 序列化候选
        candidates_serialized: list[dict[str, Any]] = [
            {
                "primary_intent": c.primary_intent.value,
                "secondary_intent": c.secondary_intent.value,
                "confidence": c.confidence,
            }
            for c in decision.candidates
        ]

        return {
            **state,
            "intent": decision.suggested_agent.value,
            "needs_clarification": outcome.needs_clarification,
            "sub_results": {
                **state.get("sub_results", {}),
                "intent_router": {
                    "primary": decision.primary_intent.value,
                    "secondary": decision.secondary_intent.value,
                    "confidence": decision.confidence,
                    "entities": decision.entities.model_dump(exclude_none=True),
                    "candidates": candidates_serialized,
                    "reason_code": decision.reason_code.value,
                    "parse_mode": outcome.parse_mode,
                    "needs_clarification": outcome.needs_clarification,
                },
            },
        }
