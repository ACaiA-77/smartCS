"""PyMySQL connection defaults and cancellation-safe thread execution.

Platform transactions and checkpoint connection-owned locks have different
lifetimes. This module shares only their infrastructure: connection settings,
environment resolution, and waiting for an in-flight worker before propagating
cancellation. Each store retains its transaction, lease and error contracts.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, TypeVar

import pymysql

T = TypeVar("T")


def connection_config(
    *,
    host: str = "127.0.0.1",
    port: int = 3307,
    database: str = "smartcs_checkpoint",
    user: str = "smartcs",
    password: str,
    autocommit: bool,
) -> dict[str, Any]:
    """Build driver settings; the caller explicitly chooses transaction mode."""
    if not password:
        raise ValueError("MYSQL_PASSWORD is required")
    return dict(
        host=host, port=int(port), database=database, user=user, password=password,
        charset="utf8mb4", autocommit=autocommit, connect_timeout=5,
        read_timeout=10, write_timeout=10, cursorclass=pymysql.cursors.DictCursor,
    )


def environment_config() -> dict[str, Any]:
    """Resolve the same MYSQL_* variables for both domain stores."""
    return dict(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3307")),
        database=os.getenv("MYSQL_DATABASE", "smartcs_checkpoint"),
        user=os.getenv("MYSQL_USER", "smartcs"),
        password=os.getenv("MYSQL_PASSWORD", ""),
    )


async def run_in_thread(function: Callable[[], T]) -> T:
    """Wait for a worker to finish before returning cancellation to its caller.

    Cancelling to_thread does not stop the underlying thread. A store must not
    release a connection or session lock while that worker is still using it.
    Driver exceptions are left to the store's own public error mapping.
    """
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise
