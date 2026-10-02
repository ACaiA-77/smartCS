"""Background UserMemoryWorker acceptance tests.

Covers: background application, restart recovery from durable PENDING rows,
lease-expiry recovery after a crashed claim, exactly-once application across
concurrent workers, clean shutdown, failure release/retry, and numeric-only
operational stats. Chat-tail behavior (enqueue-only, never awaiting
application) is covered in tests/test_context_integration.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from memory.user_memory import InMemoryUserMemoryRepository, UserMemoryService
from memory.user_memory_worker import UserMemoryWorker


def make_service():
    repository = InMemoryUserMemoryRepository()
    return repository, UserMemoryService(repository=repository)


def add_message(repository, session, event_id, content, *, user="user-a", seq=None):
    repository.add_source(
        user, session, event_id, content, seq=seq,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


async def enqueue_preference(repository, service, *, user="user-a", event_id="1",
                             session="session-1", content="Please keep it concise."):
    add_message(repository, session, event_id, content, user=user)
    result = await service.process_message(user, session, event_id, content)
    assert result["status"] == "queued"
    return result


@pytest.mark.asyncio
async def test_worker_applies_pending_candidate_in_background():
    repository, service = make_service()
    await enqueue_preference(repository, service)
    assert repository.cards == []
    assert await service.retrieve("user-a", "concise") == []

    worker = UserMemoryWorker(service)
    assert await worker.run_once() is True

    assert len(repository.cards) == 1
    recalled = await service.retrieve("user-a", "concise response", top_k=3)
    assert len(recalled) == 1
    assert recalled[0]["value_json"]["style"] == "concise"
    stats = worker.stats()
    assert stats["cycles"] == 1 and stats["claimed_total"] == 1 and stats["accepted_total"] == 1


@pytest.mark.asyncio
async def test_worker_sweep_recovers_pending_after_restart_without_new_message():
    repository, service = make_service()
    await enqueue_preference(repository, service)

    # A fresh service/worker pair simulates a process restart: only the
    # durable PENDING row remains; no in-memory scheduling survives.
    restarted_service = UserMemoryService(repository=repository)
    assert await restarted_service.pending_user_ids() == ["user-a"]
    worker = UserMemoryWorker(restarted_service)
    assert await worker.run_once() is True
    assert len(repository.cards) == 1
    assert await restarted_service.pending_user_ids() == []


@pytest.mark.asyncio
async def test_lease_expiry_recovers_candidate_left_by_crashed_claim():
    repository, service = make_service()
    await enqueue_preference(repository, service)

    # A worker claims the candidate and crashes before applying or releasing.
    claimed = await repository.claim_pending("user-a", limit=10, lease_seconds=1)
    assert len(claimed) == 1
    await asyncio.sleep(1.1)

    worker = UserMemoryWorker(service)
    assert await worker.run_once() is True
    assert len(repository.cards) == 1
    row = next(iter(repository.candidates.values()))
    assert row["decision"] != "CLAIMED"


@pytest.mark.asyncio
async def test_two_concurrent_workers_apply_one_candidate_exactly_once():
    repository, service = make_service()
    await enqueue_preference(repository, service)

    worker_a = UserMemoryWorker(service)
    worker_b = UserMemoryWorker(service)
    results = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

    assert sum(1 for worked in results if worked) == 1
    assert len(repository.cards) == 1
    assert worker_a.stats()["accepted_total"] + worker_b.stats()["accepted_total"] == 1


@pytest.mark.asyncio
async def test_worker_start_stop_is_clean_and_idempotent():
    repository, service = make_service()
    worker = UserMemoryWorker(service, poll_interval=0.05, idle_poll_interval=0.05)
    await worker.start()
    assert worker.running is True

    await asyncio.sleep(0.2)  # several idle cycles; no busy loop work to do
    await worker.stop()
    assert worker.running is False
    assert worker.stats()["stopped"] is True
    assert worker.stats()["cycles"] >= 1

    await worker.stop()  # idempotent
    assert worker.running is False


@pytest.mark.asyncio
async def test_worker_failure_releases_candidate_for_durable_retry():
    repository, service = make_service()
    await enqueue_preference(repository, service)

    original_apply = repository.apply_profile_candidate

    async def failing_apply(candidate):
        raise RuntimeError("injected apply failure")

    repository.apply_profile_candidate = failing_apply
    worker = UserMemoryWorker(service)
    assert await worker.run_once() is True  # claimed, then failed
    row = next(iter(repository.candidates.values()))
    assert row["decision"] == "PENDING"
    assert row["attempts"] == 1
    assert worker.stats()["failed_total"] == 1
    assert worker.stats()["last_error_type"] == "candidate_apply_failed"

    repository.apply_profile_candidate = original_apply
    assert await worker.run_once() is True
    assert len(repository.cards) == 1
    assert worker.stats()["accepted_total"] == 1


@pytest.mark.asyncio
async def test_worker_stats_and_logs_never_contain_memory_contents(monkeypatch, caplog):
    import logging

    repository, service = make_service()
    secret_text = "my order ORD-SECRET-42 refund 598.00"
    repository.add_source(
        "user-a", "session-1", "1", secret_text,
        seq=1, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await service.process_message("user-a", "session-1", "1", secret_text)

    worker = UserMemoryWorker(service)
    with caplog.at_level(logging.WARNING, logger="memory.user_memory_worker"):
        assert await worker.run_once() is True

    serialized = repr(worker.stats()) + caplog.text
    assert "ORD-SECRET-42" not in serialized
    assert "598.00" not in serialized
    assert set(worker.stats()) <= {
        "cycles", "users_swept", "claimed_total", "accepted_total",
        "failed_total", "last_error_type", "started_at", "stopped",
    }


@pytest.mark.asyncio
async def test_worker_constructor_rejects_unbounded_configuration():
    repository, service = make_service()
    with pytest.raises(ValueError):
        UserMemoryWorker(service, poll_interval=0)
    with pytest.raises(ValueError):
        UserMemoryWorker(service, idle_poll_interval=61, max_poll_interval=60)
    with pytest.raises(ValueError):
        UserMemoryWorker(service, batch_users=0)
    with pytest.raises(ValueError):
        UserMemoryWorker(service, per_user_limit=-1)


@pytest.mark.asyncio
async def test_worker_without_sweepable_service_stays_idle():
    class LegacyService:
        async def process_pending(self, *_args, **_kwargs):
            raise AssertionError("legacy service must not be polled without a sweep API")

    worker = UserMemoryWorker(LegacyService())
    assert await worker.run_once() is False
    assert worker.stats()["claimed_total"] == 0
