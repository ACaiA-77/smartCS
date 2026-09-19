"""Account-scoped conversation index; checkpoint content lives in CheckpointStore."""

from __future__ import annotations

import uuid

from platform_db.database import PlatformDatabase
from platform_db.users import _identity


class Sessions:
    def __init__(self, db: PlatformDatabase):
        self.db = db

    async def create(self, account_id: int, title: str = "", client_request_id: str | None = None) -> dict:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("invalid account_id")
        if not isinstance(title, str) or len(title) > 200 or "\x00" in title:
            raise ValueError("invalid session title")
        if client_request_id is not None:
            _identity(client_request_id, "client_request_id")
        session_id = str(uuid.uuid4())
        def insert(_connection, cursor):
            # DB uniqueness also handles concurrent retries of a lost initial chat response.
            cursor.execute("""INSERT INTO conversation_session
                (session_id,account_id,title,client_request_id) VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE session_id=session_id""",
                           (session_id, account_id, title, client_request_id))
            if client_request_id is not None:
                cursor.execute("""SELECT * FROM conversation_session
                    WHERE account_id=%s AND client_request_id=%s""", (account_id, client_request_id))
            else:
                cursor.execute("SELECT * FROM conversation_session WHERE session_id=%s AND account_id=%s",
                               (session_id, account_id))
            return cursor.fetchone()
        return await self.db._call(insert)

    async def get_owned(self, session_id: str, account_id: int) -> dict | None:
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM conversation_session WHERE session_id=%s AND account_id=%s",
                           (session_id, account_id))
            return cursor.fetchone()
        return await self.db._call(read)

    async def list_owned(self, account_id: int) -> list[dict]:
        def read(_connection, cursor):
            cursor.execute("""SELECT * FROM conversation_session WHERE account_id=%s
                ORDER BY updated_at DESC, session_id DESC""", (account_id,))
            return list(cursor.fetchall())
        return await self.db._call(read)

    async def delete(self, session_id: str, account_id: int) -> bool:
        def delete(_connection, cursor):
            cursor.execute("DELETE FROM conversation_session WHERE session_id=%s AND account_id=%s",
                           (session_id, account_id))
            return cursor.rowcount == 1
        return await self.db._call(delete)

    async def touch(self, session_id: str, account_id: int, title: str | None = None) -> bool:
        if title is not None and (not isinstance(title, str) or len(title) > 200 or "\x00" in title):
            raise ValueError("invalid session title")
        def update(_connection, cursor):
            cursor.execute("""UPDATE conversation_session SET updated_at=CURRENT_TIMESTAMP(6),
                title=CASE WHEN title='' THEN COALESCE(%s,title) ELSE title END
                WHERE session_id=%s AND account_id=%s""", (title, session_id, account_id))
            return cursor.rowcount == 1
        return await self.db._call(update)
