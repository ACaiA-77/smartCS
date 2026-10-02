from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from memory.user_memory import (
    CandidateKind,
    InMemoryUserMemoryRepository,
    MemoryCandidate,
    MemoryDecision,
    MemoryExtractor,
    MemoryPolicy,
    MemoryProvenanceError,
    UserMemoryService,
)


def make_service():
    repository = InMemoryUserMemoryRepository()
    return repository, UserMemoryService(repository=repository)


def add_message(repository, session, event_id, content, *, user="user-a", seq=None, at=None, event_type="USER_MESSAGE"):
    repository.add_source(
        user, session, event_id, content, seq=seq,
        created_at=at or datetime(2026, 1, 1, tzinfo=timezone.utc), event_type=event_type,
    )


@pytest.mark.asyncio
async def test_profile_is_deferred_then_recalled_across_sessions_by_same_owner():
    repository, service = make_service()
    add_message(repository, "session-1", "1", "Please keep it concise.")

    queued = await service.process_message("user-a", "session-1", "1", "Please keep it concise.")
    assert queued["status"] == "queued"
    assert queued["deferred"] is True
    assert repository.cards == []
    assert await service.retrieve("user-a", "concise answer") == []

    drained = await service.drain_pending("user-a")
    assert drained["decisions"][0]["decision"] == MemoryDecision.ADD.value
    recalled = await service.retrieve("user-a", "concise response", top_k=3)
    assert len(recalled) == 1
    assert recalled[0]["value_json"]["style"] == "concise"
    assert recalled[0]["source_session_id"] == "session-1"
    assert await service.retrieve("other-user", "concise response") == []


@pytest.mark.asyncio
async def test_provenance_requires_exact_owner_session_event_type_and_content():
    repository, service = make_service()
    add_message(repository, "owned-session", "11", "Please reply in English.")
    add_message(repository, "owned-session", "12", "Please reply in English.", event_type="ASSISTANT_MESSAGE")

    with pytest.raises(MemoryProvenanceError):
        await service.process_message("other-user", "owned-session", "11", "Please reply in English.")
    with pytest.raises(MemoryProvenanceError):
        await service.process_message("user-a", "owned-session", "999", "Please reply in English.")
    with pytest.raises(MemoryProvenanceError):
        await service.process_message("user-a", "owned-session", "12", "Please reply in English.")
    with pytest.raises(MemoryProvenanceError):
        await service.process_message("user-a", "owned-session", "11", "Please reply in Chinese.")
    assert repository.candidates == {}
    assert repository.cards == []


@pytest.mark.asyncio
async def test_compound_name_and_simplified_chinese_preferences_are_parsed_separately():
    repository, service = make_service()
    english = "Call me Bob and reply in English."
    chinese = "叫我小明以后请用简体中文回复"
    add_message(repository, "s1", "181", english, seq=1)
    add_message(repository, "s1", "182", chinese, seq=2)

    await service.process_message("user-a", "s1", "181", english)
    await service.process_pending("user-a")
    await service.process_message("user-a", "s1", "182", chinese)
    await service.process_pending("user-a")

    name = await repository.active_card("user-a", "identity", "preferred_name")
    language = await repository.active_card("user-a", "preference", "response_language")
    assert name["value_json"]["name"] == "小明"
    assert language["value_json"]["language"] == "zh-CN"
    assert len(repository.cards) == 4
    assert "reply in English" not in name["value_json"]["name"]


@pytest.mark.asyncio
async def test_synthetic_user_event_cannot_poison_stable_preferences():
    repository, service = make_service()
    content = "Please reply in English."
    repository.add_source("user-a", "sidebar-session", "13", content, synthetic=True)
    with pytest.raises(MemoryProvenanceError):
        await service.process_message("user-a", "sidebar-session", "13", content)
    assert repository.candidates == {}
    assert repository.cards == []


@pytest.mark.asyncio
async def test_business_state_is_historical_episode_only_and_lexical_recall_excludes_irrelevant():
    repository, service = make_service()
    content = "My refund request was approved yesterday."
    add_message(repository, "s1", "21", content)

    await service.process_message("user-a", "s1", "21", content)
    result = await service.process_pending("user-a")
    assert result["decisions"][0]["decision"] == MemoryDecision.ADD.value
    assert repository.cards == []

    relevant = await service.retrieve("user-a", "refund", top_k=3)
    assert len(relevant) == 1
    assert relevant[0]["memory_type"] == "episode"
    assert relevant[0]["historical"] is True
    assert relevant[0]["authoritative"] is False
    assert relevant[0]["not_authoritative"] is True
    assert "live authorized tool" in relevant[0]["source_readonly_hint"]
    assert await service.retrieve("user-a", "weather", top_k=3) == []


@pytest.mark.asyncio
async def test_profile_conflict_keeps_version_history_and_orders_by_source_time():
    repository, service = make_service()
    first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    first = "Please reply in Chinese."
    second = "Please reply in English."
    add_message(repository, "s1", "31", first, seq=1, at=first_time)
    add_message(repository, "s1", "32", second, seq=2, at=second_time)

    await service.process_message("user-a", "s1", "31", first)
    assert (await service.process_pending("user-a"))["decisions"][0]["decision"] == "ADD"
    await service.process_message("user-a", "s1", "32", second)
    assert (await service.process_pending("user-a"))["decisions"][0]["decision"] == "UPDATE"

    history = sorted(repository.cards, key=lambda card: card["version"])
    assert [card["version"] for card in history] == [1, 2]
    assert history[0]["valid_to"] == second_time.isoformat()
    assert history[1]["valid_to"] is None
    assert history[1]["value_json"]["language"] == "en"
    assert history[1]["source_created_at"] == second_time.isoformat()


@pytest.mark.asyncio
async def test_delayed_older_source_cannot_override_newer_active_preference():
    repository, service = make_service()
    older_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
    older, newer = "Please reply in Chinese.", "Please reply in English."
    add_message(repository, "old-session", "41", older, at=older_time)
    add_message(repository, "new-session", "42", newer, at=newer_time)

    await service.process_message("user-a", "new-session", "42", newer)
    await service.process_pending("user-a")
    await service.process_message("user-a", "old-session", "41", older)
    result = await service.process_pending("user-a")

    assert result["decisions"][0]["decision"] == MemoryDecision.IGNORE.value
    active = await repository.active_card("user-a", "preference", "response_language")
    assert active["value_json"]["language"] == "en"
    assert len(repository.cards) == 1


@pytest.mark.asyncio
async def test_pending_candidate_survives_worker_failure_and_retries():
    repository, service = make_service()
    content = "Please keep it concise."
    add_message(repository, "s1", "51", content)
    await service.process_message("user-a", "s1", "51", content)
    original = repository.apply_profile_candidate
    fail_once = True

    async def flaky(candidate):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated worker boundary")
        return await original(candidate)

    repository.apply_profile_candidate = flaky
    failed = await service.process_pending("user-a")
    assert failed["status"] == "partial_failure"
    assert failed["failed_count"] == 1
    assert next(iter(repository.candidates.values()))["decision"] == "PENDING"
    assert repository.cards == []
    replay = await service.process_message("user-a", "s1", "51", content)
    assert replay["existing_count"] == 1
    assert next(iter(repository.candidates.values()))["decision"] == "PENDING"

    retried = await service.process_pending("user-a")
    assert retried["decisions"][0]["decision"] == "ADD"
    assert len(repository.cards) == 1


@pytest.mark.asyncio
async def test_replay_after_apply_before_terminal_decision_is_idempotent():
    repository, service = make_service()
    content = "Please reply in English."
    add_message(repository, "s1", "61", content)
    await service.process_message("user-a", "s1", "61", content)
    original = repository.mark_candidate
    fail_once = True

    async def fail_mark_once(candidate_id, decision, reason, token):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated crash after durable profile write")
        await original(candidate_id, decision, reason, token)

    repository.mark_candidate = fail_mark_once
    first = await service.process_pending("user-a")
    assert first["failed_count"] == 1
    assert len(repository.cards) == 1
    assert next(iter(repository.candidates.values()))["decision"] == "PENDING"

    retried = await service.process_pending("user-a")
    assert retried["decisions"][0]["decision"] == "MERGE"
    assert len(repository.cards) == 1
    assert next(iter(repository.candidates.values()))["decision"] == "MERGE"


@pytest.mark.asyncio
async def test_concurrent_first_profile_writes_serialize_on_owner_key():
    repository, service = make_service()
    older_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    old_text, new_text = "Please reply in Chinese.", "Please reply in English."
    add_message(repository, "s1", "71", old_text, seq=1, at=older_time)
    add_message(repository, "s1", "72", new_text, seq=2, at=newer_time)

    await asyncio.gather(
        service.process_message("user-a", "s1", "71", old_text),
        service.process_message("user-a", "s1", "72", new_text),
    )
    await service.process_pending("user-a", limit=10)
    active = await repository.profile_cards("user-a", limit=10)
    assert len(active) == 1
    assert active[0]["value_json"]["language"] == "en"
    assert sum(card["valid_to"] is None for card in repository.cards) == 1


@pytest.mark.asyncio
async def test_sensitive_candidate_is_ignored_without_copying_secret_into_candidate():
    repository, service = make_service()
    secret_text = "Please reply in English; my password is ultra-private-987."
    add_message(repository, "s1", "81", secret_text)
    await service.process_message("user-a", "s1", "81", secret_text)

    result = await service.process_pending("user-a")
    assert result["decisions"][0]["decision"] == "IGNORE"
    assert repository.cards == []
    stored_candidate = next(iter(repository.candidates.values()))["candidate"]
    assert "ultra-private-987" not in repr(stored_candidate.value_json)


@pytest.mark.asyncio
async def test_consolidation_deduplicates_merges_and_abstraction_respects_clear_cutoff():
    repository, service = make_service()
    contents = ["My refund request was approved."] * 3
    for index, content in enumerate(contents, start=1):
        add_message(repository, "s1", str(90 + index), content, seq=index,
                    at=datetime(2026, 1, index, tzinfo=timezone.utc))
        await service.process_message("user-a", "s1", str(90 + index), content)
    await service.process_pending("user-a", limit=10)

    stats = await service.consolidate("user-a")
    assert stats["importance_scored"] >= 1
    assert stats["deduplicated"] == 2
    assert stats["merged"] == 2
    assert stats["abstracted"] == 1
    episodes = await repository.episodes("user-a")
    assert len(episodes) == 1
    assert episodes[0]["episode_type"] == "merged"
    assert "My refund request was approved." in episodes[0]["value_json"]["historical_quote"]
    assert episodes[0]["value_json"]["derived_abstraction"]["not_authoritative"] is True
    assert len(episodes[0]["source_refs"]) == 3

    repository.clear_through("user-a", "s1", seq=1)
    assert await service.retrieve("user-a", "refund") == []


@pytest.mark.asyncio
async def test_query_aware_episode_candidates_recall_old_relevant_source_after_30_newer_unrelated():
    repository, service = make_service()
    old_content = "My refund request was approved in an earlier conversation."
    add_message(repository, "archive-session", "151", old_content, seq=1,
                at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    await service.process_message("user-a", "archive-session", "151", old_content)

    for index in range(30):
        content = f"My order was shipped, unrelated update {index}."
        session = f"recent-session-{index}"
        event_id = str(160 + index)
        add_message(repository, session, event_id, content, seq=1,
                    at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index))
        await service.process_message("user-a", session, event_id, content)
    await service.process_pending("user-a", limit=100)

    recalled = await service.retrieve("user-a", "refund", top_k=1)
    assert len(recalled) == 1
    assert recalled[0]["source_event_id"] == "151"
    assert recalled[0]["source_session_id"] == "archive-session"
    assert await service.retrieve("user-a", "volcano weather", top_k=3) == []


@pytest.mark.asyncio
async def test_identifier_retrieval_keeps_old_specific_refund_searchable_after_consolidation():
    repository, service = make_service()
    old_content = (
        "Refund follow-up detail; " * 60
        + "order ORD-OLD-9041 was rejected for $73.25 on 2025-03-14."
    )
    add_message(repository, "old-case-session", "501", old_content, seq=1,
                at=datetime(2025, 3, 15, tzinfo=timezone.utc))
    await service.process_message("user-a", "old-case-session", "501", old_content)

    for index in range(30):
        date = datetime(2026, 4, 1, tzinfo=timezone.utc) + timedelta(days=index)
        content = f"My refund request for order ORD-NEW-{index:04d} was delayed on {date:%Y-%m-%d}."
        session = f"recent-refund-{index}"
        event_id = str(600 + index)
        add_message(repository, session, event_id, content, seq=1, at=date)
        await service.process_message("user-a", session, event_id, content)
    await service.process_pending("user-a", limit=100)

    before = await service.retrieve("user-a", "ORD-OLD-9041", top_k=1)
    assert len(before) == 1
    assert before[0]["source_event_id"] == "501"
    assert before[0]["source_session_id"] == "old-case-session"
    assert before[0]["historical"] is True and before[0]["authoritative"] is False
    stored_value = before[0]["value_json"]
    assert stored_value["quote_truncated"] is True
    assert len(stored_value["historical_quote"]) <= 1200
    assert stored_value["structured_refs"]["order_ids"] == ["ORD-OLD-9041"]
    assert stored_value["structured_refs"]["identifiers"] == ["ORD-OLD-9041"]
    assert stored_value["structured_refs"]["amounts"] == ["$73.25"]
    assert stored_value["structured_refs"]["dates"] == ["2025-03-14"]
    assert "ORD-OLD-9041" not in stored_value["historical_quote"]
    assert repository.cards == []

    stats = await service.consolidate("user-a")
    assert stats["archived"] == 0
    assert stats["abstracted"] == 1
    after = await service.retrieve("user-a", "ORD-OLD-9041", top_k=1)
    assert len(after) == 1
    assert after[0]["source_event_id"] == "501"
    assert after[0]["value_json"]["structured_refs"]["order_ids"] == ["ORD-OLD-9041"]
    assert len(await repository.episodes("user-a", limit=100)) == 31
    assert repository.cards == []


@pytest.mark.asyncio
async def test_episode_policy_rejects_malformed_semantics_nonfinite_confidence_and_source():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = MemoryCandidate(
        kind=CandidateKind.EPISODE, user_id="user-a", category="episode",
        key="business_state_mention",
        value_json={
            "summary": "Historical refund statement.", "historical_quote": "My refund request was delayed.",
            "quote_truncated": False, "topics": ["refund"],
            "historical": True, "not_authoritative": True, "requires_live_tool_lookup": True,
            "structured_refs": {"identifiers": []},
        },
        confidence=0.55, source_session_id="s1", source_event_id="10",
        source_seq=1, source_created_at=at,
    )
    policy = MemoryPolicy()
    assert policy.episode_valid(candidate)
    assert not policy.episode_valid(replace(candidate, confidence=float("nan")))
    assert not policy.episode_valid(replace(candidate, source_event_id="missing"))
    assert not policy.episode_valid(replace(
        candidate, value_json={**candidate.value_json, "topics": "refund"}
    ))
    assert not policy.episode_valid(replace(
        candidate, value_json={**candidate.value_json, "structured_refs": {"ids": [float("inf")]}}
    ))


@pytest.mark.asyncio
async def test_consolidation_expires_old_low_importance_episode_with_provenance():
    repository, service = make_service()
    source_time = datetime.now(timezone.utc) - timedelta(days=400)
    content = "My order was shipped."
    add_message(repository, "old-session", "99", content, seq=1, at=source_time)
    candidate = MemoryCandidate(
        kind=CandidateKind.EPISODE, user_id="user-a", category="episode",
        key="business_state_mention",
        value_json={"summary": "old order mention", "topics": ["order"],
                    "not_authoritative": True, "requires_live_tool_lookup": True},
        confidence=0.1, source_session_id="old-session", source_event_id="99",
        source_seq=1, source_created_at=source_time,
    )
    assert await repository.add_episode(candidate)

    stats = await service.consolidate("user-a")
    assert stats["expired"] == 1
    assert stats["archived"] == 1
    assert repository.episode_rows[0]["archived_at"] is not None
    assert await service.retrieve("user-a", "order") == []


@pytest.mark.asyncio
async def test_profile_survives_history_clear_until_explicit_deactivation():

    repository, service = make_service()
    content = "Please keep it concise."
    add_message(repository, "s1", "101", content, seq=1)
    await service.process_message("user-a", "s1", "101", content)
    await service.process_pending("user-a")

    repository.clear_through("user-a", "s1", 1)
    assert len(await service.retrieve("user-a", "concise")) == 1
    assert await service.deactivate_profile("user-a", "preference", "response_style") is True
    assert await service.retrieve("user-a", "concise") == []


@pytest.mark.asyncio
async def test_old_queue_entry_cannot_resurrect_deactivated_profile():
    repository, service = make_service()
    old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    text = "Please reply in English."
    add_message(repository, "s1", "111", text, at=old_time)
    await service.process_message("user-a", "s1", "111", text)
    await service.process_pending("user-a")
    assert await service.deactivate_profile("user-a", "preference", "response_language")

    # A retry of a terminal candidate remains terminal; a new delayed candidate
    # from the same source is deduplicated and cannot reactivate the card.
    await service.process_message("user-a", "s1", "111", text)
    assert await service.process_pending("user-a")=={
        "status": "processed", "user_id": "user-a", "claimed_count": 0,
        "accepted_count": 0, "failed_count": 0, "decisions": [],
    }
    assert await service.retrieve("user-a", "English") == []


@pytest.mark.asyncio
async def test_queued_preference_older_than_deactivation_is_not_resurrected():
    repository, service = make_service()
    initial_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queued_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
    initial, delayed = "Please reply in Chinese.", "Please reply in English."
    add_message(repository, "s1", "121", initial, seq=1, at=initial_time)
    add_message(repository, "s1", "122", delayed, seq=2, at=queued_time)
    await service.process_message("user-a", "s1", "121", initial)
    await service.process_pending("user-a")

    await service.process_message("user-a", "s1", "122", delayed)
    assert await service.deactivate_profile("user-a", "preference", "response_language")
    result = await service.process_pending("user-a")
    assert result["decisions"][0]["decision"] == MemoryDecision.IGNORE.value
    assert await service.retrieve("user-a", "English") == []


def test_knowledge_memory_compatibility_aliases_remain_identical():
    from memory import KnowledgeMemory, LongTermMemory
    from memory.knowledge import KnowledgeMemory as FacadeKnowledgeMemory
    from memory.long_term import KnowledgeMemory as LongTermKnowledgeMemory

    assert KnowledgeMemory is LongTermMemory is FacadeKnowledgeMemory is LongTermKnowledgeMemory


@pytest.mark.asyncio
async def test_real_mysql_memory_roundtrip_is_opt_in_and_uuid_scoped():
    if os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL disabled; set SMARTCS_CHECKPOINT_MYSQL_TEST=1 explicitly")
    from dotenv import load_dotenv
    from checkpoint.store import CheckpointStore
    from platform_db.database import PlatformDatabase

    load_dotenv()
    checkpoint = CheckpointStore.from_env()
    database = PlatformDatabase.from_env()
    await checkpoint.initialize()
    service = UserMemoryService(database=database)
    await service.initialize()
    session_id = "um-test-" + uuid.uuid4().hex
    user_id = "um-owner-" + uuid.uuid4().hex
    content = "Please keep it concise."
    try:
        event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": content})
        synthetic = await checkpoint.append_event(
            session_id, user_id, "USER_MESSAGE", {"content": "Please reply in English.", "synthetic": True}
        )
        with pytest.raises(MemoryProvenanceError):
            await service.process_message(user_id, session_id, synthetic["event_id"], "Please reply in English.")
        queued = await service.process_message(user_id, session_id, event["event_id"], content)
        assert queued["status"] == "queued"
        assert (await service.process_pending(user_id))["accepted_count"] == 1
        assert len(await service.retrieve(user_id, "concise")) == 1

        same_content = "Please keep it concise."
        same_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": same_content})
        await service.process_message(user_id, session_id, same_event["event_id"], same_content)
        assert (await service.process_pending(user_id))["decisions"][0]["decision"] == "MERGE"
        merged_card = (await service.profile_cards(user_id, limit=10))[0]
        assert len(merged_card["source_refs"]) == 2

        initial_language = "Please reply in Chinese."
        initial_language_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": initial_language})
        await service.process_message(user_id, session_id, initial_language_event["event_id"], initial_language)
        assert (await service.process_pending(user_id))["decisions"][0]["decision"] == "ADD"

        delayed_content = "Please reply in Chinese."
        delayed_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": delayed_content})
        newer_content = "Please reply in English."
        newer_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": newer_content})
        await service.process_message(user_id, session_id, newer_event["event_id"], newer_content)
        assert (await service.process_pending(user_id))["decisions"][0]["decision"] == "UPDATE"
        await service.process_message(user_id, session_id, delayed_event["event_id"], delayed_content)
        assert (await service.process_pending(user_id))["decisions"][0]["decision"] == "IGNORE"
        active_profile = (await service.profile_cards(user_id, limit=10))[0]
        assert active_profile["value_json"]["language"] == "en"

        for name in ("Alice", "Bob"):
            name_content = f"Call me {name}."
            name_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": name_content})
            await service.process_message(user_id, session_id, name_event["event_id"], name_content)
        first_worker, second_worker = await asyncio.gather(
            service.process_pending(user_id, limit=1),
            service.process_pending(user_id, limit=1),
        )
        assert first_worker["claimed_count"] + second_worker["claimed_count"] == 2
        active_name = await service.repository.active_card(user_id, "identity", "preferred_name")
        assert active_name["value_json"]["name"] == "Bob"

        old_content = "My refund request was approved in an earlier conversation."
        old_event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": old_content})
        await service.process_message(user_id, session_id, old_event["event_id"], old_content)
        for index in range(30):
            unrelated = f"My order was shipped, unrelated update {index}."
            event = await checkpoint.append_event(session_id, user_id, "USER_MESSAGE", {"content": unrelated})
            await service.process_message(user_id, session_id, event["event_id"], unrelated)
        await service.process_pending(user_id, limit=100)
        old_match = await service.retrieve(user_id, "refund", top_k=1)
        assert len(old_match) == 1
        assert old_match[0]["source_event_id"] == str(old_event["event_id"])

        await database._call(lambda _connection, cursor: cursor.execute(
            "UPDATE session_digest SET cutoff_seq=%s WHERE session_id=%s AND user_id=%s",
            (event["seq"], session_id, user_id),
        ))
        assert await service.retrieve(user_id, "refund", top_k=1) == []
        assert len(await service.retrieve(user_id, "concise", top_k=3)) == 1

        # A genuinely delayed queued update must not resurrect an explicitly
        # deactivated preference, and both prior versions remain in history.
        delayed_content = "Please reply in Chinese."
        delayed_deactivation_event = await checkpoint.append_event(
            session_id, user_id, "USER_MESSAGE", {"content": delayed_content}
        )
        await service.process_message(
            user_id, session_id, delayed_deactivation_event["event_id"], delayed_content
        )
        assert await service.deactivate_profile(user_id, "preference", "response_language") is True
        assert (await service.process_pending(user_id))["decisions"][0]["decision"] == "IGNORE"
        assert await service.repository.active_card(user_id, "preference", "response_language") is None

        def read_language_history(_connection, cursor):
            cursor.execute(
                """SELECT version,valid_to FROM user_memory_profile_card
                   WHERE user_id=%s AND category='preference' AND memory_key='response_language'
                   ORDER BY version""",
                (user_id,),
            )
            return list(cursor.fetchall())

        language_history = await database._call(read_language_history)
        assert len(language_history) >= 2
        assert all(row["valid_to"] is not None for row in language_history)

        with pytest.raises(MemoryProvenanceError):
            await service.process_message(user_id + "-wrong", session_id, event["event_id"], content)
    finally:
        # Isolated UUID owner/session only. No tables, service data, or shared rows are dropped.
        def cleanup(_connection, cursor):
            for table in ("user_memory_episode", "user_memory_candidate", "user_memory_profile_card", "user_memory_profile_lock"):
                cursor.execute(f"DELETE FROM {table} WHERE user_id=%s", (user_id,))
            # Keep append-only source events and their digest row; logically clear
            # this UUID-scoped fixture instead of deleting test history.
            cursor.execute(
                """UPDATE session_digest SET cutoff_seq=COALESCE(
                       (SELECT MAX(e.seq) FROM conversation_event AS e
                        WHERE e.session_id=%s AND e.user_id=%s),cutoff_seq)
                   WHERE session_id=%s AND user_id=%s""",
                (session_id, user_id, session_id, user_id),
            )
        await database._call(cleanup)
