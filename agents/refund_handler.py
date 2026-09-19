"""Two-step refund request handler backed by the shared safe tool executor."""

from __future__ import annotations

import re
from typing import Any

from langchain_openai import ChatOpenAI

from memory.session_store import SessionStore
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tracing.otel_config import trace_agent_call


ORDER_ID_RE = re.compile(r"(?<![A-Z0-9])ORD[-_][A-Z0-9-]+", re.IGNORECASE)
CHANGE_ORDER_RE = re.compile(r"换一个|换一笔|另一个|另一笔|其他订单|其他一单")
REFUND_MODE_LABELS = {
    "refund_only": "仅退款",
    "return_and_refund": "退货退款",
}


class RefundHandlerAgent:
    """Evaluate first, then create only after a deterministic user confirmation."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        tool_executor: ToolExecutor | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.llm = llm
        self.tool_executor = tool_executor
        self.session_store = session_store

    async def _context(self, state: dict[str, Any]) -> dict[str, Any]:
        session_id = str(state.get("session_id", "default"))
        if self.session_store is not None:
            return (await self.session_store.get_state(session_id)).to_dict()
        return dict(state.get("sub_results", {}).get("_session_context", {}) or {})

    async def _set_pending(self, session_id: str, action: dict[str, Any]) -> None:
        if self.session_store is not None:
            await self.session_store.set_pending_action(session_id, action)

    async def _clear_pending(self, session_id: str) -> None:
        if self.session_store is not None:
            await self.session_store.clear_pending_action(session_id)

    @staticmethod
    def _state_with_response(
        state: dict[str, Any], response: str, pending: dict[str, Any] | None
    ) -> dict[str, Any]:
        sub_results = dict(state.get("sub_results", {}))
        sub_results["refund_handler"] = response
        context = dict(sub_results.get("_session_context", {}) or {})
        if pending is None:
            context.pop("pending_action", None)
        else:
            context["pending_action"] = pending
        sub_results["_session_context"] = context
        return {**state, "sub_results": sub_results}

    @staticmethod
    def _order_id(
        message: str, entities: dict[str, Any], accumulated: dict[str, Any]
    ) -> str | None:
        match = ORDER_ID_RE.search(message or "")
        if match:
            return match.group(0).replace("_", "-").upper()
        if CHANGE_ORDER_RE.search(message or ""):
            return None
        # Only use the remembered order when the current turn did not name one.
        value = accumulated.get("order_id")
        return str(value).strip() if value else None

    @staticmethod
    def _reason(_message: str) -> str:
        # Free-form chat text can contain secrets or PII; the tool only needs a reason.
        return "用户申请退款"

    @staticmethod
    def _format_amount(value: Any) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _format_refund_mode(value: Any) -> str:
        if value in REFUND_MODE_LABELS:
            return REFUND_MODE_LABELS[value]
        return str(value) if value not in (None, "") else "未知"

    @staticmethod
    def _ineligible_message(order_id: str, payload: dict[str, Any]) -> str:
        reason = payload.get("reason_code")
        if reason == "order_not_owned":
            return "抱歉，当前账户无法办理该订单的退款。"
        if reason == "not_found":
            return f"未找到订单 {order_id}，请确认订单号后重试。"
        labels = {
            "not_paid": "订单尚未完成支付",
            "already_refunded": "订单已完成退款",
            "refund_already_pending": "订单已有退款申请处理中",
            "order_cancelled": "订单已取消",
        }
        return f"当前订单暂不满足退款条件：{labels.get(reason, '暂不符合退款条件')}。"

    @trace_agent_call("refund_request")
    async def _request(self, state: dict[str, Any]) -> dict[str, Any]:
        session_id = str(state.get("session_id", "default"))
        # A new request invalidates the previous confirmation before any lookup.
        await self._clear_pending(session_id)
        message = str(state.get("messages", [])[-1].content)
        info = state.get("sub_results", {}).get("intent_router", {})
        entities = info.get("entities", {}) if isinstance(info, dict) else {}
        context = await self._context(state)
        order_id = self._order_id(message, entities, context.get("accumulated_entities", {}) or {})
        user_id = str(state.get("user_id", "")).strip()
        if not order_id:
            return self._state_with_response(
                state,
                "请提供订单号，例如：帮我退款 ORD-20260801-0002。",
                None,
            )
        if not user_id or self.tool_executor is None:
            return self._state_with_response(state, "退款评估服务暂时不可用，请稍后重试。", None)

        evaluated = await self.tool_executor.execute(
            "refund_evaluate",
            {"order_id": order_id, "user_id": user_id},
            ToolExecutionContext(),
        )
        if not evaluated.success or not isinstance(evaluated.result, dict):
            return self._state_with_response(state, "退款评估暂时失败，请稍后重试。", None)
        payload = evaluated.result
        if not payload.get("eligible"):
            return self._state_with_response(state, self._ineligible_message(order_id, payload), None)

        reason = self._reason(message)
        arguments = {"order_id": order_id, "user_id": user_id, "reason": reason}
        pending = {
            "type": "refund_create",
            "order_id": order_id,
            "user_id": user_id,
            "amount": payload.get("amount"),
            "refund_mode": payload.get("refund_mode"),
            "reason": reason,
            "idempotency_key": f"refund:{session_id}:{order_id}",
            "arguments": arguments,
        }
        try:
            await self._set_pending(session_id, pending)
        except ValueError:
            return self._state_with_response(state, "退款评估结果不完整，请稍后重试。", None)
        return self._state_with_response(
            state,
            f"订单 {order_id} 可退款金额：{self._format_amount(payload.get('amount'))}，"
            f"退款方式：{self._format_refund_mode(payload.get('refund_mode'))}。如需提交，请明确回复“确认退款”；"
            "如不办理，请回复“取消退款”。",
            pending,
        )

    @trace_agent_call("refund_confirm")
    async def _confirm(self, state: dict[str, Any]) -> dict[str, Any]:
        message = str(state.get("messages", [])[-1].content)
        session_id = str(state.get("session_id", "default"))
        context = await self._context(state)
        pending = context.get("pending_action")
        if not isinstance(pending, dict):
            return self._state_with_response(state, "当前没有待确认的退款申请。", None)
        current_user_id = str(state.get("user_id", "")).strip()
        if current_user_id != str(pending.get("user_id", "")).strip():
            await self._clear_pending(session_id)
            return self._state_with_response(state, "当前退款确认信息无效，未执行退款。", None)
        requested = ORDER_ID_RE.search(message)
        if requested and requested.group(0).replace("_", "-").upper() != pending.get("order_id"):
            return self._state_with_response(
                state,
                f"本轮订单号与待确认订单 {pending.get('order_id')} 不匹配，未提交退款。",
                pending,
            )
        if self.tool_executor is None:
            return self._state_with_response(state, "退款提交服务暂时不可用，请稍后重试。", pending)

        result = await self.tool_executor.execute(
            "refund_create",
            dict(pending["arguments"]),
            ToolExecutionContext(
                confirmed=True,
                idempotency_key=pending["idempotency_key"],
            ),
        )
        if not result.success:
            return self._state_with_response(
                state,
                "退款提交暂时失败，请稍后用相同确认语句重试。",
                pending,
            )
        payload = result.result if isinstance(result.result, dict) else {}
        if not payload.get("success"):
            reason = payload.get("reason_code", "业务条件未满足")
            await self._clear_pending(session_id)
            return self._state_with_response(
                state,
                f"退款申请未提交：{reason}。当前申请已失效，如需办理请重新发起退款。",
                None,
            )
        await self._clear_pending(session_id)
        return self._state_with_response(
            state,
            f"退款申请已提交，退款单号：{payload.get('refund_id')}，"
            f"金额：{self._format_amount(payload.get('amount'))}，状态：{payload.get('status', 'pending')}。",
            None,
        )

    async def _cancel(self, state: dict[str, Any]) -> dict[str, Any]:
        session_id = str(state.get("session_id", "default"))
        await self._clear_pending(session_id)
        return self._state_with_response(state, "已取消本次退款申请。", None)

    @trace_agent_call("refund_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        info = state.get("sub_results", {}).get("intent_router", {})
        secondary = info.get("secondary") or info.get("secondary_intent")
        if secondary == "refund_confirm":
            return await self._confirm(state)
        if secondary == "refund_cancel":
            return await self._cancel(state)
        return await self._request(state)


__all__ = ["RefundHandlerAgent"]
