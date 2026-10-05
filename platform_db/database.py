"""Small thread-wrapped PyMySQL access; does not change CheckpointStore."""

from __future__ import annotations

import asyncio
import os

import pymysql


class PlatformUnavailable(RuntimeError):
    """Safe public error; never expose driver errors or SQL values."""


class PlatformConflict(ValueError):
    """A platform identity or ownership constraint rejected the operation."""


class PlatformDatabase:
    def __init__(self, *, host="127.0.0.1", port=3307, database="smartcs_checkpoint",
                 user="smartcs", password: str):
        if not password:
            raise ValueError("MYSQL_PASSWORD is required")
        self._config = dict(host=host, port=int(port), database=database, user=user,
                            password=password, charset="utf8mb4", autocommit=False,
                            connect_timeout=5, read_timeout=10, write_timeout=10,
                            cursorclass=pymysql.cursors.DictCursor)

    @classmethod
    def from_env(cls):
        return cls(host=os.getenv("MYSQL_HOST", "127.0.0.1"),
                   port=int(os.getenv("MYSQL_PORT", "3307")),
                   database=os.getenv("MYSQL_DATABASE", "smartcs_checkpoint"),
                   user=os.getenv("MYSQL_USER", "smartcs"),
                   password=os.getenv("MYSQL_PASSWORD", ""))

    async def ping(self) -> None:
        """Cheapest possible liveness proof: one connection, one `SELECT 1`.

        No DDL, no writes, no schema reads, so a readiness probe may call it on
        every check. Raises `PlatformUnavailable` when MySQL cannot be reached;
        the driver error is never surfaced (see `_call`).
        """

        def probe(_connection, cursor):
            cursor.execute("SELECT 1")
            return True

        await self._call(probe)

    async def _call(self, function):
        def work():
            try:
                connection = pymysql.connect(**self._config)
                try:
                    with connection.cursor() as cursor:
                        result = function(connection, cursor)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            except pymysql.IntegrityError:
                raise PlatformConflict("platform identity or ownership conflict") from None
            except pymysql.MySQLError:
                raise PlatformUnavailable("platform database unavailable") from None

        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread is not cancelled with its caller; finish its transaction first.
            try:
                await task
            finally:
                raise

    async def initialize(self):
        def create(_connection, cursor):
            cursor.execute("""CREATE TABLE IF NOT EXISTS platform_user (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                password_hash VARCHAR(512) NOT NULL,
                business_user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS conversation_session (
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                account_id BIGINT NOT NULL,
                title VARCHAR(200) NOT NULL DEFAULT '',
                client_request_id VARCHAR(128) COLLATE utf8mb4_bin NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY account_initial_request (account_id, client_request_id),
                KEY account_recent_session (account_id, updated_at),
                CONSTRAINT session_account_fk FOREIGN KEY (account_id) REFERENCES platform_user(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        await self._call(create)
