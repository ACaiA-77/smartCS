"""Small PyMySQL store with CAS snapshots and connection-owned session locks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pymysql
from pydantic import ValidationError

from checkpoint.models import (
    AgentCheckpoint, CheckpointConflict, CheckpointCorrupt,
    CheckpointOwnershipError, CheckpointUnavailable,
)


class CheckpointStore:
    def __init__(self, *, host="127.0.0.1", port=3307, database="smartcs_checkpoint", user="smartcs", password: str):
        if not password:
            raise ValueError("MYSQL_PASSWORD is required")
        self._config = dict(host=host, port=int(port), database=database, user=user, password=password,
                            charset="utf8mb4", autocommit=True, connect_timeout=5,
                            read_timeout=10, write_timeout=10, cursorclass=pymysql.cursors.DictCursor)
        self._lease = ContextVar(f"mysql_checkpoint_lease_{id(self)}", default=None)

    @classmethod
    def from_env(cls):
        return cls(host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3307")),
                   database=os.getenv("MYSQL_DATABASE", "smartcs_checkpoint"),
                   user=os.getenv("MYSQL_USER", "smartcs"), password=os.getenv("MYSQL_PASSWORD", ""))

    async def _thread(self, function):
        # A cancelled to_thread keeps running. Wait before releasing its connection/lock.
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise
        except pymysql.MySQLError as exc:
            raise CheckpointUnavailable("checkpoint database unavailable") from exc

    def _connection(self):
        return pymysql.connect(**self._config)

    async def _call(self, function):
        lease = self._lease.get()
        def work():
            connection = lease[0] if lease else self._connection()
            try:
                with connection.cursor() as cursor:
                    if lease:
                        cursor.execute("SELECT IS_USED_LOCK(%s) = CONNECTION_ID() AS owned", (lease[1],))
                        if not cursor.fetchone()["owned"]:
                            raise CheckpointConflict("session execution lock lost")
                    return function(connection, cursor)
            finally:
                if not lease:
                    connection.close()
        return await self._thread(work)

    async def initialize(self):
        def create(_connection, cursor):
            cursor.execute("""CREATE TABLE IF NOT EXISTS agent_checkpoint (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                version BIGINT NOT NULL, state_json JSON NOT NULL,
                status VARCHAR(32) NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            # A session snapshot is overwritten each turn; receipts retain old request deduplication.
            cursor.execute("""CREATE TABLE IF NOT EXISTS agent_checkpoint_request (
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                request_hash CHAR(64) NOT NULL, response JSON NOT NULL,
                PRIMARY KEY (session_id, request_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        await self._call(create)

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        name = hashlib.sha256(f"{self._config['database']}:{session_id}".encode()).hexdigest()
        holder = []
        def acquire():
            connection = self._connection()
            holder.append(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (name,))
                if cursor.fetchone()["acquired"] != 1:
                    raise CheckpointConflict("session is already processing a request")
            return connection
        token = None
        try:
            connection = await self._thread(acquire)
            token = self._lease.set((connection, name))
            yield
        finally:
            if token is not None:
                self._lease.reset(token)
            # Closing releases GET_LOCK even after exceptions. No expiry-based lock stealing.
            if holder:
                await self._thread(holder[0].close)

    @staticmethod
    def _decode(row, user_id):
        if row is None:
            return None
        if row["user_id"] != user_id:
            raise CheckpointOwnershipError("session belongs to another user")
        try:
            value = AgentCheckpoint.model_validate(json.loads(row["state_json"]))
            if value.version != row["version"] or value.user_id != user_id or value.session_id != row["session_id"]:
                raise ValueError("checkpoint identity/version mismatch")
            if value.status != row["status"]:
                raise ValueError("checkpoint status mismatch")
            return value
        except (ValidationError, ValueError, TypeError) as exc:
            raise CheckpointCorrupt("invalid checkpoint snapshot") from exc

    async def load(self, session_id: str, user_id: str):
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM agent_checkpoint WHERE session_id=%s", (session_id,))
            return self._decode(cursor.fetchone(), user_id)
        return await self._call(read)

    async def save(self, checkpoint: AgentCheckpoint):
        value = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "version": 1})
        def insert(_connection, cursor):
            try:
                cursor.execute("INSERT INTO agent_checkpoint (session_id,user_id,version,state_json,status) VALUES (%s,%s,%s,%s,%s)",
                               (value.session_id, value.user_id, value.version, value.payload(), value.status))
            except pymysql.err.IntegrityError as exc:
                raise CheckpointConflict("session already exists") from exc
            return value
        return await self._call(insert)

    async def update(self, checkpoint: AgentCheckpoint):
        value = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "version": checkpoint.version + 1})
        def update(connection, cursor):
            connection.begin()
            try:
                cursor.execute("""UPDATE agent_checkpoint SET state_json=%s,version=%s,status=%s
                    WHERE session_id=%s AND user_id=%s AND version=%s""",
                               (value.payload(), value.version, value.status, value.session_id, value.user_id, checkpoint.version))
                if cursor.rowcount != 1:
                    raise CheckpointConflict("checkpoint version changed")
                if value.status in {"finished", "waiting"} and value.context.get("request_id"):
                    state = value.context["state"]
                    response = {"final_response": state["final_response"], "intent": value.intent,
                                "compliance_passed": state["compliance_passed"],
                                "client_request_id": value.context["request_id"], "session_id": value.session_id}
                    cursor.execute("""INSERT IGNORE INTO agent_checkpoint_request
                        (session_id,request_id,user_id,request_hash,response) VALUES (%s,%s,%s,%s,%s)""",
                                   (value.session_id, value.context["request_id"], value.user_id,
                                    value.context["request_hash"], json.dumps(response, ensure_ascii=False, allow_nan=False)))
                connection.commit()
                return value
            except BaseException:
                connection.rollback()
                raise
        return await self._call(update)

    async def receipt(self, session_id, user_id, request_id, request_hash):
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM agent_checkpoint_request WHERE session_id=%s AND request_id=%s",
                           (session_id, request_id))
            row = cursor.fetchone()
            if row is None:
                return None
            if row["user_id"] != user_id:
                raise CheckpointOwnershipError("session belongs to another user")
            if row["request_hash"] != request_hash:
                raise CheckpointConflict("request id reused with different content")
            return json.loads(row["response"])
        return await self._call(read)

    async def delete(self, session_id, user_id, version):
        def delete(connection, cursor):
            connection.begin()
            try:
                cursor.execute("DELETE FROM agent_checkpoint WHERE session_id=%s AND user_id=%s AND version=%s",
                               (session_id, user_id, version))
                if cursor.rowcount != 1:
                    raise CheckpointConflict("checkpoint version changed")
                cursor.execute("DELETE FROM agent_checkpoint_request WHERE session_id=%s", (session_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        await self._call(delete)
