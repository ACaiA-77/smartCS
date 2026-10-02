"""Assemble prioritized context blocks into LangChain messages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from context.budget import TokenCounter
from context.compression import compact_blocks_near_limit
from context.models import ContextBlock, ContextCompressionError, ContextOverflowError, ContextPackage, ModelProfile


STATIC_ORDER = {
    "System": 0,
    "ToolSchemas": 1,
}
DYNAMIC_ORDER = {
    "UserProfileCards": 10,
    "RetrievedUserMemory": 12,
    "SessionState": 20,
    "Evidence": 30,
    "Summary": 40,
    "RecentHistory": 50,
    "CurrentUser": 60,
    "StatusBar": 70,
    "ProtectedFields": 80,
}


def _block_text(block: ContextBlock) -> str:
    return f"<{block.name}>\n{block.content}\n</{block.name}>" if block.content else ""


class ContextAssembler:
    """Budget against the messages actually sent, including framing overhead."""

    def __init__(self, profile: ModelProfile, token_counter: TokenCounter) -> None:
        self.profile = profile
        self.token_counter = token_counter

    def prepare_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        prepared = [deepcopy(block) for block in blocks if str(block.content or "").strip()]
        for block in prepared:
            block.tokens = self.token_counter.count_text(_block_text(block))
        return prepared

    @staticmethod
    def _messages_for(blocks: list[ContextBlock]) -> list[Any]:
        system_parts: list[str] = []
        human_parts: list[str] = []
        for block in sorted(blocks, key=lambda item: item.order):
            text = _block_text(block)
            if not text:
                continue
            if block.name in STATIC_ORDER:
                system_parts.append(text)
            else:
                human_parts.append(text)
        messages: list[Any] = []
        if system_parts:
            messages.append(SystemMessage(content="\n\n".join(system_parts)))
        if human_parts:
            messages.append(HumanMessage(content="\n\n".join(human_parts)))
        return messages

    def _measure(self, blocks: list[ContextBlock]) -> int:
        return self.token_counter.count_messages(self._messages_for(blocks))

    def pack(self, blocks: list[ContextBlock], diagnostics: dict[str, Any]) -> list[ContextBlock]:
        prepared = self.prepare_blocks(blocks)
        required = [block for block in prepared if block.required or block.priority in (0, 1)]
        required_tokens = self._measure(required)
        diagnostics["context_prompt_budget"] = self.profile.prompt_budget
        diagnostics["context_soft_limit"] = self.profile.soft_limit
        diagnostics["context_required_tokens"] = required_tokens
        if required_tokens > self.profile.prompt_budget:
            diagnostics["overflow"] = {
                "required_tokens": required_tokens,
                "prompt_budget": self.profile.prompt_budget,
                "required_blocks": {block.name: block.tokens for block in required},
            }
            raise ContextOverflowError("required context blocks exceed prompt budget")

        current = prepared
        actual = self._measure(current)
        # L3 rolling/history compaction begins at the soft threshold. It does not
        # trim evidence or change any persisted event/digest.
        if actual > self.profile.soft_limit:
            current = compact_blocks_near_limit(
                current,
                profile=self.profile,
                token_counter=self.token_counter,
                diagnostics=diagnostics,
                target_tokens=self.profile.soft_limit,
                hard=False,
                measure=self._measure,
            )
            actual = self._measure(current)

        # L5 is reserved for a real hard-budget breach, after soft rolling.
        if actual > self.profile.prompt_budget:
            current = compact_blocks_near_limit(
                current,
                profile=self.profile,
                token_counter=self.token_counter,
                diagnostics=diagnostics,
                target_tokens=self.profile.prompt_budget,
                hard=True,
                measure=self._measure,
            )
            actual = self._measure(current)

        if actual > self.profile.prompt_budget:
            required_ids = {id(block) for block in current if block.required or block.priority in (0, 1)}
            optional = [block for block in current if id(block) not in required_ids]
            # Lower-priority additions are evicted first. At equal priority,
            # user memory and status are less important than retrieved/tool evidence.
            drop_rank = {
                "RetrievedUserMemory": 0,
                "UserProfileCards": 1,
                "StatusBar": 2,
                "Summary": 3,
                "RecentHistory": 4,
                "Evidence": 5,
            }
            optional.sort(key=lambda block: (-block.priority, drop_rank.get(block.name, 5), -block.tokens))
            for victim in optional:
                current.remove(victim)
                diagnostics.setdefault("dropped_blocks", {})[victim.name] = {
                    "tokens": victim.tokens,
                    "priority": victim.priority,
                    "reason": "hard_prompt_budget",
                }
                actual = self._measure(current)
                if actual <= self.profile.prompt_budget:
                    break

        if actual > self.profile.prompt_budget:
            diagnostics["overflow"] = {
                "actual_message_tokens": actual,
                "prompt_budget": self.profile.prompt_budget,
            }
            raise ContextCompressionError("context remains over budget after bounded compression and optional block eviction")
        diagnostics["context_tokens_after_pack"] = actual
        return sorted(current, key=lambda block: block.order)

    def to_package(
        self,
        blocks: list[ContextBlock],
        *,
        diagnostics: dict[str, Any],
        protected_fields: dict[str, Any] | None = None,
    ) -> ContextPackage:
        packed = self.pack(blocks, diagnostics)
        messages = self._messages_for(packed)
        total_tokens = self.token_counter.count_messages(messages)
        # Final accounting is over the exact LangChain messages returned below,
        # never the sum of per-block estimates.
        if total_tokens > self.profile.prompt_budget:
            diagnostics["overflow"] = {
                "actual_message_tokens": total_tokens,
                "prompt_budget": self.profile.prompt_budget,
            }
            raise ContextCompressionError("final assembled messages exceed prompt budget")
        block_tokens = {block.name: block.tokens for block in packed}
        diagnostics["context_tokens_total"] = total_tokens
        diagnostics["context_block_ratio"] = {
            name: (tokens / total_tokens if total_tokens else 0.0)
            for name, tokens in block_tokens.items()
        }
        diagnostics["block_tokens"] = block_tokens
        diagnostics["final_prompt_within_budget"] = total_tokens <= self.profile.prompt_budget
        return ContextPackage(
            messages=messages,
            total_tokens=total_tokens,
            block_tokens=block_tokens,
            diagnostics=diagnostics,
            protected_fields=protected_fields or {},
        )


__all__ = ["ContextAssembler", "DYNAMIC_ORDER", "STATIC_ORDER"]
