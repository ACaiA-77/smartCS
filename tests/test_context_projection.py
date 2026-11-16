"""Pure projection rules extracted from ContextManager."""

from __future__ import annotations

from types import SimpleNamespace

from context.projection import (
    checkpoint_projection,
    checkpoint_session_state,
    format_messages,
    merge_checkpoint_messages,
    safe_state,
)


def test_safe_state_keeps_scalars_pending_action_and_critical_entities_only():
    safe = safe_state({
        "last_intent": "refund",
        "turn_count": 3,
        "messages": ["not persisted"],
        "private_state": {"secret": True},
        "pending_action": {"type": "refund", "order_id": "ORD-1"},
        "accumulated_entities": {
            "order_id": "ORD-1",
            "note": "not critical",
            "nested": {"order_id": "ignored"},
        },
    })
    assert safe["last_intent"] == "refund" and safe["turn_count"] == 3
    assert safe["pending_action"] == {"type": "refund", "order_id": "ORD-1"}
    assert safe["accumulated_entities"] == {"order_id": "ORD-1"}
    assert "messages" not in safe and "private_state" not in safe


def test_checkpoint_null_pending_action_is_authoritative_after_cancel():
    checkpoint = SimpleNamespace(
        context={"session_state": {"pending_action": {"type": "refund"}}},
        pending_action=None,
    )
    assert checkpoint_session_state(checkpoint)["pending_action"] is None


def test_merge_appends_a_new_request_even_when_messages_match_older_ones():
    old = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "您好"}]
    new = [{"role": "user", "content": "你好"}]
    merged = merge_checkpoint_messages(
        old, new, request_id="req-2", prior_request_id="req-1"
    )
    assert merged == [*old, *new]


def test_merge_collapses_a_same_request_overlap():
    old = [{"role": "user", "content": "退款"}]
    new = [{"role": "user", "content": "退款"}]
    merged = merge_checkpoint_messages(
        old, new, request_id="req-1", prior_request_id="req-1"
    )
    assert merged == old


def test_merge_keeps_a_snapshot_that_extends_the_cached_history_once():
    old = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    new = [*old, {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]
    merged = merge_checkpoint_messages(
        old, new, request_id="req-2", prior_request_id="req-1"
    )
    assert merged == new


def test_merge_limits_the_retained_window():
    old = [{"role": "user", "content": f"old-{index}"} for index in range(25)]
    new = [{"role": "user", "content": "new"}]
    merged = merge_checkpoint_messages(
        old, new, request_id="req-2", prior_request_id="req-1", limit=20
    )
    assert len(merged) == 20 and merged[-1]["content"] == "new"


def test_format_messages_keeps_known_roles_only_and_applies_the_limit():
    messages = [
        {"role": "user", "content": "u"},
        {"role": "system", "content": "dropped"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": ""},
    ]
    assert format_messages(messages, limit=5) == [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]


def test_checkpoint_projection_marks_cold_restore_and_carries_cutoff():
    checkpoint = SimpleNamespace(
        context={"request_id": "req-1", "session_state": {"turn_count": 1}, "cutoff_seq": 7},
        messages=[{"role": "user", "content": "hi"}],
        last_event_seq=9,
        version=2,
    )
    projection = checkpoint_projection(checkpoint, "s-1", "u-1")
    assert projection["requires_cold_restore"] is True
    assert projection["cutoff_seq"] == 7
    assert projection["summary_event_seq"] == 7
    assert projection["checkpoint_version"] == 2
    assert projection["synchronized_request_id"] == "req-1"
    assert projection["recent_messages"] == [{"role": "user", "content": "hi"}]
