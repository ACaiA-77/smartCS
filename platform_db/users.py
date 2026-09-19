"""Internal account rows, including hashes: never serialize rows directly to customers."""

from __future__ import annotations

from platform_db.database import PlatformDatabase


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip() or "\x00" in value:
        raise ValueError(f"invalid {name}")
    return value


class Users:
    def __init__(self, db: PlatformDatabase):
        self.db = db

    async def by_id(self, account_id: int) -> dict | None:
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM platform_user WHERE id=%s", (account_id,))
            return cursor.fetchone()
        return await self.db._call(read)

    async def by_username(self, username: str) -> dict | None:
        try:
            _identity(username, "username")
        except ValueError:
            return None
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM platform_user WHERE username=%s", (username,))
            return cursor.fetchone()
        return await self.db._call(read)

    async def create(self, username: str, password_hash: str, business_user_id: str) -> dict:
        _identity(username, "username")
        _identity(business_user_id, "business_user_id")
        if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$") or len(password_hash) > 512:
            raise ValueError("an Argon2id password hash is required")
        def insert(_connection, cursor):
            cursor.execute("""INSERT INTO platform_user (username,password_hash,business_user_id)
                VALUES (%s,%s,%s)""", (username, password_hash, business_user_id))
            cursor.execute("SELECT * FROM platform_user WHERE id=%s", (cursor.lastrowid,))
            return cursor.fetchone()
        return await self.db._call(insert)
