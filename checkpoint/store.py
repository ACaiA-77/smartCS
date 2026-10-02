"""PyMySQL checkpoint/event store with CAS, durable history, and connection-owned locks.

Legacy checkpoint snapshots contain at most the final 20 messages because the
pre-event orchestrator truncated them before storage. Migration imports those
available messages once; it cannot recover turns that were already truncated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import pymysql
from pydantic import ValidationError

from checkpoint.models import (
    AgentCheckpoint,
    CheckpointConflict,
    CheckpointCorrupt,
    CheckpointMessage,
    CheckpointOwnershipError,
    CheckpointUnavailable,
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
        if lease:
            # Child tasks inherit the same lease ContextVar; never use its single
            # PyMySQL connection concurrently from multiple to_thread workers.
            async with lease[2]:
                return await self._thread(work)
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
            self._ensure_column(cursor, "agent_checkpoint", "last_event_seq", "BIGINT NOT NULL DEFAULT 0")
            # A session snapshot is overwritten each turn; receipts retain old request deduplication.
            cursor.execute("""CREATE TABLE IF NOT EXISTS agent_checkpoint_request (
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                request_hash CHAR(64) NOT NULL, response JSON NOT NULL,
                PRIMARY KEY (session_id, request_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS conversation_event (
                event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                seq BIGINT NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                event_key VARCHAR(255) COLLATE utf8mb4_bin NULL,
                payload JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_conversation_event_seq (session_id, seq),
                UNIQUE KEY uq_conversation_event_key (session_id, event_key),
                KEY idx_conversation_event_owner (session_id, user_id, seq)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS session_digest (
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
                version BIGINT NOT NULL DEFAULT 0,
                last_event_seq BIGINT NOT NULL DEFAULT 0,
                summary_event_seq BIGINT NOT NULL DEFAULT 0,
                cutoff_seq BIGINT NOT NULL DEFAULT 0,
                rolling_summary TEXT NULL,
                archive_summary TEXT NULL,
                protected_fields JSON NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                KEY idx_session_digest_owner (session_id, user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        await self._call(create)

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] == 1060:
                return
            raise

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        name = self._lock_name(session_id)
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
            token = self._lease.set((connection, name, asyncio.Lock()))
            yield
        finally:
            if token is not None:
                self._lease.reset(token)
            # Closing releases GET_LOCK even after exceptions. No expiry-based lock stealing.
            if holder:
                await self._thread(holder[0].close)

    def _lock_name(self, session_id: str) -> str:
        return hashlib.sha256(f"{self._config['database']}:{session_id}".encode()).hexdigest()

    @staticmethod
    def _json_dump(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CheckpointCorrupt("invalid JSON payload") from exc

    @staticmethod
    def _json_load(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise CheckpointCorrupt("invalid stored JSON") from exc
        return value

    @classmethod
    def _summary_load(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            return cls._json_load(value)
        except CheckpointCorrupt:
            # Older deployments may have stored plain-text summaries in TEXT columns.
            return value

    @classmethod
    def _decode(cls, row, user_id):
        if row is None:
            return None
        if row["user_id"] != user_id:
            raise CheckpointOwnershipError("session belongs to another user")
        try:
            raw = cls._json_load(row["state_json"])
            if "last_event_seq" not in raw:
                raw["last_event_seq"] = int(row.get("last_event_seq") or 0)
            value = AgentCheckpoint.model_validate(raw)
            if value.version != row["version"] or value.user_id != user_id or value.session_id != row["session_id"]:
                raise ValueError("checkpoint identity/version mismatch")
            if value.status != row["status"]:
                raise ValueError("checkpoint status mismatch")
            if int(row.get("last_event_seq") or 0) != value.last_event_seq:
                raise ValueError("checkpoint event cursor mismatch")
            return value
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CheckpointCorrupt("invalid checkpoint snapshot") from exc

    @staticmethod
    def _message_to_event_type(message: dict[str, Any]) -> str:
        return "USER_MESSAGE" if message["role"] == "user" else "ASSISTANT_MESSAGE"

    @staticmethod
    def _event_to_message(row: dict[str, Any]) -> dict[str, str] | None:
        payload = CheckpointStore._json_load(row["payload"])
        if not isinstance(payload, dict):
            raise CheckpointCorrupt("invalid message event payload")
        event_type = row["event_type"]
        if event_type == "MESSAGE":
            role = payload.get("role")
        elif event_type == "USER_MESSAGE":
            role = "user"
        elif event_type == "ASSISTANT_MESSAGE":
            role = "assistant"
        else:
            return None
        content = payload.get("content")
        if not isinstance(role, str) or role not in {"user", "assistant"} or not isinstance(content, str):
            raise CheckpointCorrupt("invalid message event")
        return {"role": role, "content": content}

    def _owner_for_session(self, cursor, session_id: str) -> str | None:
        cursor.execute("SELECT user_id FROM agent_checkpoint WHERE session_id=%s", (session_id,))
        owners = {row["user_id"] for row in cursor.fetchall()}
        cursor.execute("SELECT user_id FROM agent_checkpoint_request WHERE session_id=%s", (session_id,))
        owners.update(row["user_id"] for row in cursor.fetchall())
        cursor.execute("SELECT user_id FROM session_digest WHERE session_id=%s", (session_id,))
        owners.update(row["user_id"] for row in cursor.fetchall())
        cursor.execute("SELECT DISTINCT user_id FROM conversation_event WHERE session_id=%s", (session_id,))
        owners.update(row["user_id"] for row in cursor.fetchall())
        if len(owners) > 1:
            raise CheckpointCorrupt("session owner records disagree")
        return next(iter(owners), None)

    def _validate_owner(self, cursor, session_id: str, user_id: str) -> None:
        if any(not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value or value != value.strip()
               for value in (session_id, user_id)):
            raise CheckpointCorrupt("invalid session owner identity")
        owner = self._owner_for_session(cursor, session_id)
        if owner is not None and owner != user_id:
            raise CheckpointOwnershipError("session belongs to another user")

    def _max_seq(self, cursor, session_id: str) -> int:
        cursor.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM conversation_event WHERE session_id=%s", (session_id,))
        return int(cursor.fetchone()["seq"] or 0)

    def _cutoff_seq(self, cursor, session_id: str) -> int:
        cursor.execute("SELECT cutoff_seq FROM session_digest WHERE session_id=%s", (session_id,))
        row = cursor.fetchone()
        return int(row["cutoff_seq"] or 0) if row else 0

    def _latest_event_seq_for_update(self, cursor, session_id: str) -> int:
        # Locking read observes the latest committed row even when the transaction's
        # repeatable-read snapshot predates a concurrent append.
        cursor.execute("SELECT seq FROM conversation_event WHERE session_id=%s ORDER BY seq DESC LIMIT 1 FOR UPDATE",
                       (session_id,))
        row = cursor.fetchone()
        return int(row["seq"]) if row else 0

    def _lock_existing_digest_tx(self, cursor, session_id: str, user_id: str) -> dict[str, Any] | None:
        """Lock an existing digest mutex without creating an owner anchor on reads."""
        cursor.execute("SELECT * FROM session_digest WHERE session_id=%s FOR UPDATE", (session_id,))
        row = cursor.fetchone()
        if row is not None and row["user_id"] != user_id:
            raise CheckpointOwnershipError("session belongs to another user")
        return row

    def _ensure_digest(self, cursor, session_id: str, user_id: str) -> int:
        # The digest is also the permanent session-owner anchor and per-session
        # sequence mutex. Never change its owner on duplicate-key conflicts.
        cursor.execute("""INSERT INTO session_digest
            (session_id,user_id,version,last_event_seq,summary_event_seq,cutoff_seq,protected_fields)
            VALUES (%s,%s,0,0,0,0,%s)
            ON DUPLICATE KEY UPDATE session_id=VALUES(session_id)""", (session_id, user_id, "{}"))
        cursor.execute("SELECT user_id,last_event_seq FROM session_digest WHERE session_id=%s FOR UPDATE", (session_id,))
        row = cursor.fetchone()
        if row is None:
            raise CheckpointCorrupt("session digest disappeared")
        if row["user_id"] != user_id:
            raise CheckpointOwnershipError("session belongs to another user")
        latest_seq = self._latest_event_seq_for_update(cursor, session_id)
        last_event_seq = max(int(row["last_event_seq"]), latest_seq)
        if last_event_seq != int(row["last_event_seq"]):
            cursor.execute("UPDATE session_digest SET last_event_seq=%s,version=version+1 WHERE session_id=%s AND user_id=%s",
                           (last_event_seq, session_id, user_id))
        return last_event_seq

    def _append_event_tx(
        self,
        cursor,
        session_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
        event_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event_type, str) or not event_type or len(event_type) > 64:
            raise CheckpointCorrupt("invalid event type")
        if not isinstance(payload, dict):
            raise CheckpointCorrupt("event payload must be an object")
        if event_key is not None and (
            not isinstance(event_key, str) or not event_key or len(event_key) > 255 or "\x00" in event_key
        ):
            raise CheckpointCorrupt("invalid event key")
        self._validate_owner(cursor, session_id, user_id)
        payload_json = self._json_dump(payload)
        canonical_payload = json.loads(payload_json)
        last_event_seq = self._ensure_digest(cursor, session_id, user_id)
        if event_key is not None:
            cursor.execute("SELECT * FROM conversation_event WHERE session_id=%s AND event_key=%s", (session_id, event_key))
            existing = cursor.fetchone()
            if existing:
                existing_payload = self._json_load(existing["payload"])
                if existing["user_id"] != user_id or existing["event_type"] != event_type or existing_payload != canonical_payload:
                    raise CheckpointConflict("event key reused with different content")
                return {"event_id": existing["event_id"], "seq": int(existing["seq"]),
                        "event_type": existing["event_type"], "payload": existing_payload}
        seq = last_event_seq + 1
        cursor.execute("""INSERT INTO conversation_event
            (session_id,user_id,seq,event_type,event_key,payload) VALUES (%s,%s,%s,%s,%s,%s)""",
                       (session_id, user_id, seq, event_type, event_key, payload_json))
        event_id = cursor.lastrowid
        cursor.execute("""UPDATE session_digest SET last_event_seq=%s,version=version+1
            WHERE session_id=%s AND user_id=%s""", (seq, session_id, user_id))
        if cursor.rowcount != 1:
            raise CheckpointConflict("digest owner changed")
        return {"event_id": event_id, "seq": seq, "event_type": event_type, "payload": canonical_payload}

    def _message_history_tx(self, cursor, session_id: str, *, after_cutoff: bool = True, limit: int | None = None) -> list[dict[str, str]]:
        cutoff = self._cutoff_seq(cursor, session_id) if after_cutoff else 0
        limit_sql = "" if limit is None else " LIMIT %s"
        params: tuple[Any, ...] = (session_id, cutoff) if limit is None else (session_id, cutoff, int(limit))
        cursor.execute("""SELECT event_type,payload FROM conversation_event
            WHERE session_id=%s AND seq>%s AND event_type IN ('USER_MESSAGE','ASSISTANT_MESSAGE','MESSAGE')
            ORDER BY seq""" + limit_sql, params)
        rows = cursor.fetchall()
        messages = []
        for row in rows:
            message = self._event_to_message(row)
            if message is not None:
                messages.append(message)
        return messages

    def _recent_message_history_tx(
        self, cursor, session_id: str, limit: int = 20, *, cutoff_seq: int | None = None
    ) -> list[dict[str, str]]:
        cutoff = self._cutoff_seq(cursor, session_id) if cutoff_seq is None else cutoff_seq
        cursor.execute("""SELECT event_type,payload FROM (
                SELECT seq,event_type,payload FROM conversation_event
                WHERE session_id=%s AND seq>%s AND event_type IN ('USER_MESSAGE','ASSISTANT_MESSAGE','MESSAGE')
                ORDER BY seq DESC LIMIT %s
            ) AS recent ORDER BY seq""", (session_id, cutoff, int(limit)))
        rows = cursor.fetchall()
        messages = []
        for row in rows:
            message = self._event_to_message(row)
            if message is not None:
                messages.append(message)
        return messages

    @staticmethod
    def _common_existing_suffix(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> int:
        max_len = min(len(existing), len(incoming))
        for length in range(max_len, -1, -1):
            if existing[-length:] == incoming[:length] if length else True:
                return length
        return 0

    def _message_event_key(
        self, checkpoint: AgentCheckpoint, role: str, content: str, index: int, cutoff_seq: int
    ) -> str:
        request_id = checkpoint.context.get("request_id")
        if request_id is None:
            request_id = "no-request"
        elif not isinstance(request_id, str) or not request_id or len(request_id) > 128 or "\x00" in request_id:
            raise CheckpointCorrupt("invalid checkpoint request id")
        digest = hashlib.sha256(content.encode()).hexdigest()[:24]
        return f"message:{request_id}:{cutoff_seq}:{index}:{role}:{digest}"

    def _message_count_tx(self, cursor, session_id: str, user_id: str) -> int:
        cutoff = self._cutoff_seq(cursor, session_id)
        cursor.execute("""SELECT COUNT(*) AS count FROM conversation_event
            WHERE session_id=%s AND user_id=%s AND seq>%s
                AND event_type IN ('USER_MESSAGE','ASSISTANT_MESSAGE','MESSAGE')""",
                       (session_id, user_id, cutoff))
        return int(cursor.fetchone()["count"] or 0)

    def _append_message_deltas_tx(
        self, cursor, checkpoint: AgentCheckpoint, *, previous_request_id: str | None = None
    ) -> int:
        incoming = [message.model_dump() for message in checkpoint.messages]
        if not incoming:
            return self._max_seq(cursor, checkpoint.session_id)
        existing_tail = self._recent_message_history_tx(cursor, checkpoint.session_id, len(incoming))
        matched = self._common_existing_suffix(existing_tail, incoming)
        request_id = checkpoint.context.get("request_id")
        request_changed = isinstance(request_id, str) and request_id != previous_request_id
        # A request-scoped snapshot identical to the current history is a new
        # conversational turn when its request id changes, not a checkpoint retry.
        repeats_current_history = len(existing_tail) == len(incoming) == matched
        append_from = incoming if request_changed and repeats_current_history else incoming[matched:]
        if not append_from:
            return self._max_seq(cursor, checkpoint.session_id)
        absolute_start = self._message_count_tx(cursor, checkpoint.session_id, checkpoint.user_id) + 1
        cutoff_seq = self._cutoff_seq(cursor, checkpoint.session_id)
        for offset, message in enumerate(append_from):
            index = absolute_start + offset
            payload = {"role": message["role"], "content": message["content"]}
            self._append_event_tx(
                cursor,
                checkpoint.session_id,
                checkpoint.user_id,
                self._message_to_event_type(message),
                payload,
                event_key=self._message_event_key(
                    checkpoint, message["role"], message["content"], index, cutoff_seq
                ),
            )
        return self._max_seq(cursor, checkpoint.session_id)

    def _append_state_events_tx(self, cursor, checkpoint: AgentCheckpoint) -> int:
        request_id = checkpoint.context.get("request_id")
        if request_id is None:
            return self._max_seq(cursor, checkpoint.session_id)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128 or "\x00" in request_id:
            raise CheckpointCorrupt("invalid checkpoint request id")
        state = checkpoint.context.get("state", {})
        if checkpoint.current_stage in {"ROUTED", "EXECUTING", "GENERATED", "REVIEWED", "WAIT_CONFIRM", "FINISHED"}:
            intent = checkpoint.intent or (state.get("intent") if isinstance(state, dict) else "") or ""
            if intent and not isinstance(intent, str):
                raise CheckpointCorrupt("invalid checkpoint intent")
            if intent:
                self._append_event_tx(
                    cursor,
                    checkpoint.session_id,
                    checkpoint.user_id,
                    "INTENT_ROUTED",
                    {"request_id": request_id, "intent": intent},
                    event_key=f"intent:{request_id}:{hashlib.sha256(intent.encode()).hexdigest()[:24]}",
                )
        payload = {
            "request_id": request_id,
            "stage": checkpoint.current_stage,
            "status": checkpoint.status,
            "intent": checkpoint.intent,
            "session_state": checkpoint.context.get("session_state", {}),
        }
        payload_key = hashlib.sha256(self._json_dump(payload).encode("utf-8")).hexdigest()[:24]
        self._append_event_tx(
            cursor,
            checkpoint.session_id,
            checkpoint.user_id,
            "STATE_CHANGE",
            payload,
            event_key=f"state:{request_id}:{checkpoint.current_stage}:{payload_key}",
        )
        return self._max_seq(cursor, checkpoint.session_id)

    def _migrate_legacy_messages_tx(self, cursor, checkpoint: AgentCheckpoint) -> int:
        # The legacy orchestrator stored only its final 20 messages. Import that
        # already-truncated snapshot once; earlier conversation cannot be recovered.
        if not checkpoint.messages:
            return self._max_seq(cursor, checkpoint.session_id)
        legacy_messages = [message.model_dump() for message in checkpoint.messages]
        existing_messages = self._message_history_tx(
            cursor, checkpoint.session_id, after_cutoff=False
        )
        if existing_messages:
            if len(existing_messages) >= len(legacy_messages) and existing_messages[-len(legacy_messages):] == legacy_messages:
                return self._max_seq(cursor, checkpoint.session_id)
            raise CheckpointCorrupt("legacy snapshot conflicts with existing event history")
        for index, payload in enumerate(legacy_messages, start=1):
            digest = hashlib.sha256(payload["content"].encode()).hexdigest()[:24]
            self._append_event_tx(
                cursor,
                checkpoint.session_id,
                checkpoint.user_id,
                self._message_to_event_type(payload),
                payload,
                event_key=f"legacy:{checkpoint.session_id}:message:{index}:{payload['role']}:{digest}",
            )
        return self._max_seq(cursor, checkpoint.session_id)

    def _persist_checkpoint_tx(self, cursor, checkpoint: AgentCheckpoint, *, expected_version: int | None, insert: bool) -> AgentCheckpoint:
        self._validate_owner(cursor, checkpoint.session_id, checkpoint.user_id)
        self._ensure_digest(cursor, checkpoint.session_id, checkpoint.user_id)
        previous_request_id = None
        if not insert:
            cursor.execute("""SELECT user_id,version,state_json FROM agent_checkpoint
                WHERE session_id=%s FOR UPDATE""", (checkpoint.session_id,))
            existing = cursor.fetchone()
            if existing is None or existing["version"] != expected_version:
                raise CheckpointConflict("checkpoint version changed")
            if existing["user_id"] != checkpoint.user_id:
                raise CheckpointOwnershipError("session belongs to another user")
            state = self._json_load(existing["state_json"])
            if not isinstance(state, dict):
                raise CheckpointCorrupt("invalid checkpoint snapshot")
            context = state.get("context", {})
            if not isinstance(context, dict):
                raise CheckpointCorrupt("invalid checkpoint context")
            previous_request_id = context.get("request_id")
        self._append_message_deltas_tx(cursor, checkpoint, previous_request_id=previous_request_id)
        self._append_state_events_tx(cursor, checkpoint)
        last_event_seq = self._ensure_digest(cursor, checkpoint.session_id, checkpoint.user_id)
        persisted = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "messages": [], "last_event_seq": last_event_seq})
        if insert:
            try:
                cursor.execute("""INSERT INTO agent_checkpoint
                    (session_id,user_id,version,state_json,status,last_event_seq) VALUES (%s,%s,%s,%s,%s,%s)""",
                               (persisted.session_id, persisted.user_id, persisted.version,
                                persisted.payload(), persisted.status, persisted.last_event_seq))
            except pymysql.err.IntegrityError as exc:
                raise CheckpointConflict("session already exists") from exc
        else:
            cursor.execute("""UPDATE agent_checkpoint SET state_json=%s,version=%s,status=%s,last_event_seq=%s
                WHERE session_id=%s AND user_id=%s AND version=%s""",
                           (persisted.payload(), persisted.version, persisted.status, persisted.last_event_seq,
                            persisted.session_id, persisted.user_id, expected_version))
            if cursor.rowcount != 1:
                raise CheckpointConflict("checkpoint version changed")
        cursor.execute("UPDATE session_digest SET last_event_seq=GREATEST(last_event_seq,%s) WHERE session_id=%s",
                       (last_event_seq, persisted.session_id))
        # Return a workflow-compatible checkpoint while the stored JSON remains message-free.
        return AgentCheckpoint.model_validate({**persisted.model_dump(), "messages": checkpoint.messages})

    def _hydrate_checkpoint_tx(self, cursor, checkpoint: AgentCheckpoint) -> AgentCheckpoint:
        self._migrate_legacy_messages_tx(cursor, checkpoint)
        last_event_seq = self._ensure_digest(cursor, checkpoint.session_id, checkpoint.user_id)
        if last_event_seq != checkpoint.last_event_seq:
            checkpoint = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "last_event_seq": last_event_seq})
            persisted = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "messages": []})
            cursor.execute("""UPDATE agent_checkpoint SET state_json=%s,last_event_seq=%s
                WHERE session_id=%s AND user_id=%s AND version=%s""",
                           (persisted.payload(), persisted.last_event_seq, persisted.session_id,
                            persisted.user_id, persisted.version))
            if cursor.rowcount != 1:
                raise CheckpointConflict("checkpoint version changed while hydrating")
        messages = [CheckpointMessage.model_validate(item) for item in self._recent_message_history_tx(cursor, checkpoint.session_id, 20)]
        return AgentCheckpoint.model_validate({**checkpoint.model_dump(), "messages": messages})

    async def load(self, session_id: str, user_id: str):
        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                # Do not create a digest/owner anchor just because an absent
                # checkpoint was queried. Existing sessions still serialize on it.
                cursor.execute("SELECT 1 FROM agent_checkpoint WHERE session_id=%s", (session_id,))
                checkpoint_exists = cursor.fetchone() is not None
                if not checkpoint_exists:
                    self._lock_existing_digest_tx(cursor, session_id, user_id)
                    connection.commit()
                    return None

                # All checkpoint row locks follow the digest mutex in this transaction.
                self._ensure_digest(cursor, session_id, user_id)
                cursor.execute("SELECT * FROM agent_checkpoint WHERE session_id=%s FOR UPDATE", (session_id,))
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                value = self._decode(row, user_id)
                value = self._hydrate_checkpoint_tx(cursor, value)
                connection.commit()
                return value
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def save(self, checkpoint: AgentCheckpoint):
        value = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "version": 1})
        def insert(connection, cursor):
            connection.begin()
            try:
                saved = self._persist_checkpoint_tx(cursor, value, expected_version=None, insert=True)
                connection.commit()
                return saved
            except BaseException:
                connection.rollback()
                raise
        return await self._call(insert)

    def _save_receipt_tx(self, cursor, checkpoint: AgentCheckpoint) -> None:
        request_id = checkpoint.context.get("request_id")
        request_hash = checkpoint.context.get("request_hash")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128 or "\x00" in request_id:
            raise CheckpointCorrupt("invalid completed request id")
        if not isinstance(request_hash, str) or len(request_hash) != 64:
            raise CheckpointCorrupt("invalid completed request hash")
        try:
            int(request_hash, 16)
        except ValueError as exc:
            raise CheckpointCorrupt("invalid completed request hash") from exc
        state = checkpoint.context.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("final_response"), str) or type(state.get("compliance_passed")) is not bool:
            raise CheckpointCorrupt("completed request response is incomplete")
        response = {
            "final_response": state["final_response"],
            "intent": checkpoint.intent,
            "compliance_passed": state["compliance_passed"],
            "client_request_id": request_id,
            "session_id": checkpoint.session_id,
        }
        response_json = self._json_dump(response)
        try:
            cursor.execute("""INSERT INTO agent_checkpoint_request
                (session_id,request_id,user_id,request_hash,response) VALUES (%s,%s,%s,%s,%s)""",
                           (checkpoint.session_id, request_id, checkpoint.user_id, request_hash, response_json))
            return
        except pymysql.err.IntegrityError:
            cursor.execute("""SELECT user_id,request_hash,response FROM agent_checkpoint_request
                WHERE session_id=%s AND request_id=%s FOR UPDATE""", (checkpoint.session_id, request_id))
            existing = cursor.fetchone()
            if existing is None:
                raise CheckpointConflict("completed request receipt could not be saved")
            if existing["user_id"] != checkpoint.user_id:
                raise CheckpointOwnershipError("session belongs to another user")
            if existing["request_hash"] != request_hash:
                raise CheckpointConflict("request id reused with different content")
            if self._json_load(existing["response"]) != response:
                raise CheckpointConflict("completed request response changed")

    async def update(self, checkpoint: AgentCheckpoint):
        value = AgentCheckpoint.model_validate({**checkpoint.model_dump(), "version": checkpoint.version + 1})
        def update(connection, cursor):
            connection.begin()
            try:
                saved = self._persist_checkpoint_tx(cursor, value, expected_version=checkpoint.version, insert=False)
                if value.status in {"finished", "waiting"} and value.context.get("request_id"):
                    self._save_receipt_tx(cursor, value)
                connection.commit()
                return saved
            except BaseException:
                connection.rollback()
                raise
        return await self._call(update)

    async def append_event(
        self, session_id: str, user_id: str, event_type: str, payload: dict[str, Any], *, event_key: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CheckpointCorrupt("event payload must be an object")
        def append(connection, cursor):
            connection.begin()
            try:
                event = self._append_event_tx(cursor, session_id, user_id, event_type, payload, event_key)
                connection.commit()
                return event
            except BaseException:
                connection.rollback()
                raise
        return await self._call(append)

    async def history(self, session_id, user_id) -> list[dict]:
        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                messages = self._message_history_tx(cursor, session_id)
                connection.commit()
                return messages
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    @classmethod
    def _event_record(cls, row) -> dict[str, Any]:
        payload = cls._json_load(row["payload"])
        if not isinstance(payload, dict):
            raise CheckpointCorrupt("invalid event payload")
        return {"event_id": row["event_id"], "seq": int(row["seq"]),
                "event_type": row["event_type"], "payload": payload,
                "created_at": row["created_at"].isoformat()}

    async def recent_events(
        self, session_id: str, user_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        bounded_limit = min(limit, 1000)
        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cutoff = self._cutoff_seq(cursor, session_id)
                cursor.execute("""SELECT event_id,seq,event_type,payload,created_at FROM (
                    SELECT event_id,seq,event_type,payload,created_at FROM conversation_event
                    WHERE session_id=%s AND user_id=%s AND seq>%s ORDER BY seq DESC LIMIT %s
                ) AS recent ORDER BY seq""",
                               (session_id, user_id, max(after_seq, cutoff), bounded_limit))
                events = [self._event_record(row) for row in cursor.fetchall()]
                connection.commit()
                return events
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def get_event(self, session_id: str, user_id: str, event_id: int) -> dict[str, Any] | None:
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("event_id must be a positive integer")

        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cutoff = self._cutoff_seq(cursor, session_id)
                cursor.execute("""SELECT event_id,seq,event_type,payload,created_at FROM conversation_event
                    WHERE session_id=%s AND user_id=%s AND event_id=%s AND seq>%s""",
                               (session_id, user_id, event_id, cutoff))
                row = cursor.fetchone()
                event = None if row is None else self._event_record(row)
                connection.commit()
                return event
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def events_range(
        self, session_id: str, user_id: str, *, after_seq: int = 0,
        before_seq: int | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read the next contiguous event page after an exclusive seq cursor.

        ``before_seq`` is an optional inclusive upper bound. Unlike recent_events,
        this method always orders ascending so callers can advance a summarization cursor.
        """
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if before_seq is not None and (type(before_seq) is not int or before_seq < 0):
            raise ValueError("before_seq must be a non-negative integer")
        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("limit must be an integer from 0 through 1000")

        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cutoff = self._cutoff_seq(cursor, session_id)
                query = """SELECT event_id,seq,event_type,payload,created_at FROM conversation_event
                    WHERE session_id=%s AND user_id=%s AND seq>%s"""
                params: list[Any] = [session_id, user_id, max(after_seq, cutoff)]
                if before_seq is not None:
                    query += " AND seq<=%s"
                    params.append(before_seq)
                query += " ORDER BY seq ASC LIMIT %s"
                params.append(limit)
                cursor.execute(query, tuple(params))
                events = [self._event_record(row) for row in cursor.fetchall()]
                connection.commit()
                return events
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def load_working_set(self, session_id: str, user_id: str, *, recent_limit: int = 8) -> dict[str, Any]:
        if type(recent_limit) is not int:
            raise ValueError("recent_limit must be an integer")
        bounded_limit = max(0, min(recent_limit, 100))

        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cursor.execute("SELECT 1 FROM agent_checkpoint WHERE session_id=%s", (session_id,))
                checkpoint_exists = cursor.fetchone() is not None
                if checkpoint_exists:
                    self._ensure_digest(cursor, session_id, user_id)
                else:
                    self._lock_existing_digest_tx(cursor, session_id, user_id)

                # Keep a consistent lock order: owner validation, digest mutex, then
                # checkpoint row. Missing checkpoints do not create a new owner anchor.
                cp_row = None
                if checkpoint_exists:
                    cursor.execute("SELECT * FROM agent_checkpoint WHERE session_id=%s FOR UPDATE", (session_id,))
                    cp_row = cursor.fetchone()
                session_state: dict[str, Any] = {}
                checkpoint_version = 0
                if cp_row is not None:
                    checkpoint = self._decode(cp_row, user_id)
                    checkpoint = self._hydrate_checkpoint_tx(cursor, checkpoint)
                    checkpoint_version = checkpoint.version
                    session_state = checkpoint.context.get("session_state", {})
                    if not isinstance(session_state, dict):
                        raise CheckpointCorrupt("invalid checkpoint session state")

                cursor.execute("SELECT * FROM session_digest WHERE session_id=%s FOR UPDATE", (session_id,))
                digest = cursor.fetchone()
                cutoff = int(digest["cutoff_seq"]) if digest else 0
                messages = self._recent_message_history_tx(
                    cursor, session_id, bounded_limit, cutoff_seq=cutoff
                )
                last_seq = max(self._max_seq(cursor, session_id), int(digest["last_event_seq"])) if digest else self._max_seq(cursor, session_id)
                cutoff = int(digest["cutoff_seq"]) if digest else 0
                if digest is None:
                    working_set = {
                        "session_id": session_id, "user_id": user_id,
                        "recent_messages": messages, "rolling_summary": None, "archive_summary": None,
                        "session_state": session_state, "last_event_seq": last_seq, "version": 0,
                        "checkpoint_version": checkpoint_version, "protected_fields": {},
                        "summary_event_seq": 0,
                    }
                else:
                    protected_fields = self._json_load(digest["protected_fields"])
                    if not isinstance(protected_fields, dict):
                        raise CheckpointCorrupt("invalid digest protected fields")
                    summary_event_seq = int(digest["summary_event_seq"])
                    if summary_event_seq < cutoff or summary_event_seq > last_seq:
                        raise CheckpointCorrupt("invalid digest summary watermark")
                    working_set = {
                        "session_id": session_id, "user_id": user_id,
                        "recent_messages": messages,
                        "rolling_summary": digest["rolling_summary"],
                        "archive_summary": self._summary_load(digest["archive_summary"]),
                        "session_state": session_state,
                        "last_event_seq": max(int(digest["last_event_seq"]), last_seq),
                        "version": int(digest["version"]),
                        "checkpoint_version": checkpoint_version,
                        "protected_fields": protected_fields,
                        "summary_event_seq": summary_event_seq,
                    }
                # Trusted lifecycle metadata lets cache synchronization distinguish
                # current-message projections from old requests and clear epochs.
                working_set["cutoff_seq"] = cutoff
                working_set["synchronized_request_id"] = (
                    checkpoint.context.get("request_id") if cp_row is not None else None
                )
                connection.commit()
                return working_set
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def save_digest(
        self,
        session_id: str,
        user_id: str,
        *,
        rolling_summary: str | None = None,
        archive_summary: Any = None,
        protected_fields: dict[str, Any] | None = None,
        summary_event_seq: int | None = None,
    ) -> None:
        if rolling_summary is not None and not isinstance(rolling_summary, str):
            raise CheckpointCorrupt("rolling_summary must be text")
        if protected_fields is not None and not isinstance(protected_fields, dict):
            raise CheckpointCorrupt("protected_fields must be an object")
        if summary_event_seq is not None and (type(summary_event_seq) is not int or summary_event_seq < 0):
            raise CheckpointConflict("invalid digest event cursor")
        archive_json = None if archive_summary is None else self._json_dump(archive_summary)
        protected_json = None if protected_fields is None else self._json_dump(protected_fields)

        def save(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                current_last = self._ensure_digest(cursor, session_id, user_id)
                cursor.execute("SELECT * FROM session_digest WHERE session_id=%s FOR UPDATE", (session_id,))
                row = cursor.fetchone()
                if row is None or row["user_id"] != user_id:
                    raise CheckpointOwnershipError("session belongs to another user")
                cutoff = int(row["cutoff_seq"])
                old_summary_seq = int(row["summary_event_seq"])
                next_summary_seq = old_summary_seq if summary_event_seq is None else summary_event_seq
                if next_summary_seq < max(old_summary_seq, cutoff) or next_summary_seq > current_last:
                    raise CheckpointConflict("digest summary watermark is stale or ahead of events")
                if summary_event_seq is None and (
                    (rolling_summary is not None and rolling_summary != row["rolling_summary"])
                    or (archive_json is not None and archive_json != row["archive_summary"])
                ):
                    raise CheckpointConflict("summary updates require an explicit event watermark")
                fields = {
                    "rolling_summary": row["rolling_summary"] if rolling_summary is None else rolling_summary,
                    "archive_summary": row["archive_summary"] if archive_json is None else archive_json,
                    "protected_fields": row["protected_fields"] if protected_json is None else protected_json,
                }
                cursor.execute("""UPDATE session_digest SET version=version+1,last_event_seq=%s,summary_event_seq=%s,
                    rolling_summary=%s,archive_summary=%s,protected_fields=%s WHERE session_id=%s AND user_id=%s""",
                               (current_last, next_summary_seq, fields["rolling_summary"], fields["archive_summary"],
                                fields["protected_fields"], session_id, user_id))
                if cursor.rowcount != 1:
                    raise CheckpointConflict("digest owner changed")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        await self._call(save)

    async def receipt(self, session_id: str, user_id: str, request_id: str, request_hash: str):
        def read(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cursor.execute("SELECT * FROM agent_checkpoint_request WHERE session_id=%s AND request_id=%s",
                               (session_id, request_id))
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                if row["user_id"] != user_id:
                    raise CheckpointOwnershipError("session belongs to another user")
                if row["request_hash"] != request_hash:
                    raise CheckpointConflict("request id reused with different content")
                response = self._json_load(row["response"])
                if not isinstance(response, dict):
                    raise CheckpointCorrupt("invalid completed request receipt")
                connection.commit()
                return response
            except BaseException:
                connection.rollback()
                raise
        return await self._call(read)

    async def delete(self, session_id: str, user_id: str, version: int) -> None:
        def delete(connection, cursor):
            connection.begin()
            try:
                self._validate_owner(cursor, session_id, user_id)
                cursor.execute("SELECT 1 FROM agent_checkpoint WHERE session_id=%s", (session_id,))
                if cursor.fetchone() is None:
                    raise CheckpointConflict("checkpoint version changed")
                # Acquire the per-session mutex before the checkpoint row, matching
                # save/update/load/hydration and avoiding a digest<->checkpoint cycle.
                last_event_seq = self._ensure_digest(cursor, session_id, user_id)
                cursor.execute("""SELECT status FROM agent_checkpoint
                    WHERE session_id=%s AND user_id=%s AND version=%s FOR UPDATE""",
                               (session_id, user_id, version))
                row = cursor.fetchone()
                if row is None:
                    raise CheckpointConflict("checkpoint version changed")
                if row["status"] == "running":
                    raise CheckpointConflict("unfinished request cannot be deleted")
                event = self._append_event_tx(
                    cursor, session_id, user_id, "HISTORY_CLEARED", {},
                    event_key=f"clear:{version}:{last_event_seq}",
                )
                cursor.execute("""UPDATE session_digest SET cutoff_seq=%s,summary_event_seq=%s,
                    rolling_summary=NULL,archive_summary=NULL,protected_fields=%s,
                    last_event_seq=GREATEST(last_event_seq,%s)
                    WHERE session_id=%s AND user_id=%s""",
                               (event["seq"], event["seq"], "{}", event["seq"], session_id, user_id))
                if cursor.rowcount != 1:
                    raise CheckpointConflict("digest owner changed")
                cursor.execute("DELETE FROM agent_checkpoint WHERE session_id=%s AND user_id=%s AND version=%s",
                               (session_id, user_id, version))
                if cursor.rowcount != 1:
                    raise CheckpointConflict("checkpoint version changed")
                cursor.execute("DELETE FROM agent_checkpoint_request WHERE session_id=%s AND user_id=%s",
                               (session_id, user_id))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        await self._call(delete)
