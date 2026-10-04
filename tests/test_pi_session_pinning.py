"""Phase 7 §1/§5 (P7-3, P7-4): harness_version is fixed at session creation.

The hard constraint this file pins down: a session's harness is decided once,
when the row is inserted, and nothing afterwards may rewrite it — not a retry
of the creating request, not a rollout percentage change, not a title update.
"""

from __future__ import annotations

import pytest

from platform_db.sessions import Sessions
from tests.internal_api_helpers import apply_migration, platform_database, random_username, seed_account


async def _sessions() -> Sessions:
    database = platform_database()
    await database.initialize()
    return Sessions(database)


async def _account(prefix: str) -> int:
    apply_migration()
    return seed_account(random_username(prefix), f"bu-{prefix}")


@pytest.mark.asyncio
async def test_create_defaults_to_legacy():
    account_id = await _account("legacy-default")
    created = await (await _sessions()).create(account_id)
    assert created["harness_version"] == "legacy"


@pytest.mark.asyncio
async def test_create_writes_the_requested_harness_version():
    account_id = await _account("pin-pi")
    sessions = await _sessions()
    created = await sessions.create(account_id, harness_version="pi")
    assert created["harness_version"] == "pi"
    stored = await sessions.get_owned(created["session_id"], account_id)
    assert stored["harness_version"] == "pi"


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_harness_version():
    account_id = await _account("pin-bad")
    sessions = await _sessions()
    with pytest.raises(ValueError):
        await sessions.create(account_id, harness_version="v2")


@pytest.mark.asyncio
async def test_a_retry_of_the_creating_request_keeps_the_original_pin():
    """P7-4's core: the same client_request_id lands on the same row, and the
    row keeps the harness it was created with — a later rollout flip must not
    migrate an existing conversation between transcript systems."""
    account_id = await _account("pin-retry")
    sessions = await _sessions()
    first = await sessions.create(account_id, client_request_id="initial-request", harness_version="legacy")
    retry = await sessions.create(account_id, client_request_id="initial-request", harness_version="pi")
    assert retry["session_id"] == first["session_id"]
    assert retry["harness_version"] == "legacy"
    assert (await sessions.get_owned(first["session_id"], account_id))["harness_version"] == "legacy"


@pytest.mark.asyncio
async def test_touch_never_rewrites_the_harness_version():
    """A chat turn touches the session; touching is not a routing decision."""
    account_id = await _account("pin-touch")
    sessions = await _sessions()
    created = await sessions.create(account_id, harness_version="pi")
    assert await sessions.touch(created["session_id"], account_id, title="新标题") is True
    stored = await sessions.get_owned(created["session_id"], account_id)
    assert stored["harness_version"] == "pi"
    assert stored["title"] == "新标题"


@pytest.mark.asyncio
async def test_two_accounts_can_hold_different_harnesses_side_by_side():
    """P7-5 at the storage layer: cohorts coexist without interfering."""
    account_id = await _account("pin-mixed")
    sessions = await _sessions()
    pi_session = await sessions.create(account_id, harness_version="pi")
    legacy_session = await sessions.create(account_id, harness_version="legacy")
    owned = {row["session_id"]: row["harness_version"] for row in await sessions.list_owned(account_id)}
    assert owned[pi_session["session_id"]] == "pi"
    assert owned[legacy_session["session_id"]] == "legacy"
