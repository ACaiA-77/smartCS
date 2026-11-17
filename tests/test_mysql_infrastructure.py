"""Contracts for the platform MySQL transaction adapter."""

from __future__ import annotations

import asyncio
import threading

import pymysql
import pytest

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
    "driver_error,public_error",
    [
        (pymysql.OperationalError, PlatformUnavailable),
        (pymysql.IntegrityError, PlatformConflict),
    ],
)
async def test_store_keeps_its_public_error_contract(monkeypatch, driver_error, public_error):
    def cannot_connect(**_config):
        raise driver_error(999, "private database detail")

    monkeypatch.setattr(pymysql, "connect", cannot_connect)
    store = PlatformDatabase(password="test-only-password")
    with pytest.raises(public_error) as error:
        await store._call(lambda _connection, _cursor: None)
    assert "private database detail" not in str(error.value)


def test_store_rejects_missing_password(monkeypatch):
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MYSQL_PASSWORD is required"):
        PlatformDatabase.from_env()
