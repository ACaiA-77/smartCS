"""Real MySQL acceptance data, scoped to UUID accounts.

Never use live business identities: parallel acceptance generates UUID accounts
and removes all created sessions and rows on teardown.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import secrets
import uuid

from auth.password import hash_password
from platform_db.database import PlatformDatabase
from platform_db.sessions import Sessions
from platform_db.users import Users


@dataclass
class AuthTestData:
    database: PlatformDatabase
    accounts: list[dict] = field(default_factory=list, repr=False)
    passwords: list[str] = field(default_factory=list, repr=False)

    def credentials(self, index: int) -> dict[str, str]:
        return {"username": self.accounts[index]["username"], "password": self.passwords[index]}


@asynccontextmanager
async def auth_test_data(_repository=None):
    from dotenv import load_dotenv

    load_dotenv()
    database = PlatformDatabase.from_env()
    await database.initialize()
    data = AuthTestData(database)
    users = Users(database)
    prefix = "auth-accept-" + uuid.uuid4().hex
    try:
        for index in range(2):
            identity = f"{prefix}-{index}"
            password = secrets.token_urlsafe(24)
            account = await users.create(identity, hash_password(password), identity)
            data.accounts.append(account)
            data.passwords.append(password)
        yield data
    finally:
        for account in data.accounts:
            for session in await Sessions(database).list_owned(account["id"]):
                sid = session["session_id"]
                await Sessions(database).delete(sid, account["id"])
            await database._call(
                lambda _c, cursor, account=account: cursor.execute(
                    "DELETE FROM platform_user WHERE id=%s AND username=%s AND business_user_id=%s",
                    (account["id"], account["username"], account["business_user_id"]),
                )
            )
