"""Contracts shared by the platform transaction and checkpoint lock adapters."""

from __future__ import annotations

import asyncio
import threading

import pymysql
import pytest

from checkpoint.models import CheckpointUnavailable
from checkpoint.store import CheckpointStore
from db.mysql import run_in_thread
from platform_db.database import PlatformConflict, PlatformDatabase, PlatformUnavailable


@pytest.mark.asyncio
async def test_cancellation_waits_for_the_database_worker_to_finish():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work():
        started.set()
        assert release.wait(5), "test did not release worker"
        finished.set()

    task = asyncio.create_task(run_in_thread(work))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        # Let the cancellation handler run before checking resource lifetime.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store_type,driver_error,public_error",
    [
        (PlatformDatabase, pymysql.OperationalError, PlatformUnavailable),
        (PlatformDatabase, pymysql.IntegrityError, PlatformConflict),
        (CheckpointStore, pymysql.OperationalError, CheckpointUnavailable),
    ],
)
async def test_store_keeps_its_public_error_contract(monkeypatch, store_type, driver_error, public_error):
    def cannot_connect(**_config):
        raise driver_error(999, "private database detail")

    monkeypatch.setattr(pymysql, "connect", cannot_connect)
    store = store_type(password="test-only-password")
    with pytest.raises(public_error) as error:
        await store._call(lambda _connection, _cursor: None)
    assert "private database detail" not in str(error.value)


@pytest.mark.parametrize("store_type", [PlatformDatabase, CheckpointStore])
def test_both_stores_reject_missing_password(monkeypatch, store_type):
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MYSQL_PASSWORD is required"):
        store_type.from_env()
