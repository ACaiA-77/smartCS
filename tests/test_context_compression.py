from __future__ import annotations

import json

from context.assembler import ContextAssembler
from context.budget import TokenCounter
from context.compression import (
    archive_from_events,
    extract_protected_fields,
    merge_archives,
    rolling_summary_from_events,
    tool_result_preview,
)
from context.models import ContextBlock, ModelProfile


def _byte_tokenizer(text: str) -> list[int]:
    return list(text.encode("utf-8"))


def test_uppercase_checkpoint_message_events_keep_role_and_provenance() -> None:
    events = [
        {
            "seq": 21,
            "event_id": 9001,
            "event_type": "USER_MESSAGE",
            "payload": {"content": "请查询订单 A-100"},
        },
        {
            "seq": 22,
            "event_id": 9002,
            "event_type": "ASSISTANT_MESSAGE",
            "payload": {"content": "订单已找到"},
        },
    ]

    summary = rolling_summary_from_events(events)
    archive = archive_from_events(events)

    assert "user: 请查询订单 A-100" in summary
    assert "assistant: 订单已找到" in summary
    assert "seq=21" in summary and "event_id=9001" in summary
    assert archive["provenance"] == [
        {"seq": 21, "event_id": 9001, "event_type": "USER_MESSAGE"},
        {"seq": 22, "event_id": 9002, "event_type": "ASSISTANT_MESSAGE"},
    ]


def test_tool_preview_normalizes_uppercase_and_preserves_critical_fields() -> None:
    event = {
        "seq": 42,
        "event_id": 7,
        "event_type": "TOOL_RESULT",
        "payload": {
            "tool_name": "get_order",
            "status": "success",
            "result": {
                "order_id": "ORD-42",
                "amount": 15.50,
                "content": "x" * 5000,
                "debug": {"token": "must be dropped"},
            },
        },
    }

    preview = tool_result_preview(event, max_chars=160)

    assert preview["event_type"] == "tool_result"
    assert preview["name"] == "get_order"
    assert preview["success"] is True
    assert preview["seq"] == 42
    assert preview["protected_fields"]["result"]["order_id"] == "ORD-42"
    assert preview["protected_fields"]["result"]["amount"] == 15.50
    assert preview["preview_truncated"] is True
    assert "must be dropped" not in preview["preview"]
    assert preview["full_payload_durable"] is True


def test_protected_path_merge_handles_scalar_and_nested_collisions() -> None:
    protected = extract_protected_fields(
        {
            "amount": {"order_id": "ORD-1"},
            "items": [{"ticket_id": "T-1", "amount": 9}],
        }
    )

    assert protected["amount"]["order_id"] == "ORD-1"
    assert protected["items"]["0"]["ticket_id"] == "T-1"
    assert protected["items"]["0"]["amount"] == 9


def test_archive_merge_never_discards_prior_archive_or_provenance() -> None:
    first = archive_from_events([
        {"seq": 1, "event_id": "e1", "event_type": "INTENT_ROUTED", "payload": {"fact": "early fact"}}
    ])
    second = archive_from_events([
        {"seq": 2, "event_id": "e2", "event_type": "TOOL_RESULT", "payload": {"decision": "confirmed"}}
    ])

    merged = merge_archives(first, second)

    assert merged["confirmed_facts"] == ["early fact"]
    assert merged["important_decisions"] == ["confirmed"]
    assert [item["seq"] for item in merged["provenance"]] == [1, 2]


def test_hard_compaction_is_bounded_and_evidence_compacts_only_near_hard_limit() -> None:
    profile = ModelProfile(
        context_limit=1400,
        max_output_tokens=100,
        reserve=50,
        safety_margin=50,
        hard_ratio=0.9,
        soft_ratio=0.7,
        compression_attempts=5,
    )
    counter = TokenCounter(profile, _byte_tokenizer)
    diagnostics: dict = {"compression_count": 0, "compression_attempts": 0}
    evidence = [{"source": f"doc-{index}", "content": "evidence " * 65} for index in range(6)]
    package = ContextAssembler(profile, counter).to_package(
        [
            ContextBlock("System", "stable prefix", 0, 0, required=True),
            ContextBlock("Evidence", json.dumps({"retrieval": evidence}), 2, 30),
            ContextBlock("CurrentUser", "answer using this evidence", 0, 60, required=True),
        ],
        diagnostics=diagnostics,
    )

    assert diagnostics["compression_count"] > 0
    assert "L5-evidence" in diagnostics["compression_layers"]
    assert package.total_tokens <= profile.prompt_budget
    assert package.diagnostics["final_prompt_within_budget"] is True
