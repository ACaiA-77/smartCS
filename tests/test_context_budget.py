from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from context.assembler import ContextAssembler
from context.budget import TokenCounter
from context.models import ContextBlock, ModelProfile


def _byte_tokenizer(text: str) -> list[int]:
    """Deterministic test tokenizer; production never uses this fixture."""
    return list(text.encode("utf-8"))


def test_profile_validates_reservations_and_soft_hard_thresholds() -> None:
    with pytest.raises(ValueError, match="prompt budget"):
        ModelProfile(context_limit=256, max_output_tokens=200, reserve=20, safety_margin=20)
    with pytest.raises(ValueError, match="soft_ratio"):
        ModelProfile(soft_ratio=0.9, hard_ratio=0.8)


def test_assembler_final_messages_fit_actual_budget_not_block_sum() -> None:
    profile = ModelProfile(
        name="deterministic-test",
        provider="test",
        context_limit=2000,
        max_output_tokens=100,
        reserve=100,
        safety_margin=100,
        soft_ratio=0.70,
        hard_ratio=0.95,
        compression_attempts=12,
    )
    counter = TokenCounter(profile, _byte_tokenizer)
    diagnostics: dict = {"compression_count": 0, "compression_attempts": 0}
    package = ContextAssembler(profile, counter).to_package(
        [
            ContextBlock("System", "stable system prompt", 0, 0, required=True),
            ContextBlock("CurrentUser", "需要查询订单", 0, 60, required=True),
            ContextBlock("RecentHistory", "[" + ",".join(["history"] * 50) + "]", 3, 50),
        ],
        diagnostics=diagnostics,
    )

    actual = counter.count_messages(package.messages)
    assert actual == package.total_tokens
    assert actual <= profile.prompt_budget
    assert package.diagnostics["final_prompt_within_budget"] is True
    assert isinstance(package.messages[0], SystemMessage)
    assert isinstance(package.messages[-1], HumanMessage)


def test_tiktoken_treats_untrusted_special_marker_as_plain_text() -> None:
    profile = ModelProfile(name="gpt-4o-mini", provider="openai")
    counter = TokenCounter(profile)

    assert counter.count_text("quoted marker: <|endoftext|> is user text") > 0
    assert counter.diagnostics["tokenizer"].startswith("tiktoken:")


def test_unknown_provider_precision_is_reported_as_estimate() -> None:
    counter = TokenCounter(ModelProfile(name="kimi-k2", provider="moonshot"))
    requested_runtime_name = TokenCounter(ModelProfile(name="gpt-6-luna/xhigh", provider="unknown"))

    assert counter.diagnostics["tokenizer_precision"] == "tiktoken_estimate_not_native"
    assert "not verified" in counter.diagnostics["tokenizer_warning"]
    assert requested_runtime_name.diagnostics["tokenizer_precision"] == "tiktoken_estimate_not_native"
    assert "not a native provider tokenizer" in requested_runtime_name.diagnostics["tokenizer_warning"]
