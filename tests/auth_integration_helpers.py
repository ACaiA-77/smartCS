"""Real MySQL acceptance data, scoped to UUID accounts and a caller's temp SQLite.

Never use live business identities: the temporary sandbox maps sample orders to
UUID identities, so parallel acceptance cannot claim or delete a real account.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import secrets
import uuid

from auth.password import hash_password
from checkpoint.store import CheckpointStore
from platform_db.database import PlatformDatabase
from platform_db.sessions import Sessions
from platform_db.users import Users


@dataclass
class AuthTestData:
    database: PlatformDatabase
    checkpoint: CheckpointStore
    accounts: list[dict] = field(default_factory=list, repr=False)
    passwords: list[str] = field(default_factory=list, repr=False)

    def credentials(self, index):
        return {"username": self.accounts[index]["username"], "password": self.passwords[index]}


@asynccontextmanager
async def auth_test_data(repository):
    from dotenv import load_dotenv
    load_dotenv()
    database = PlatformDatabase.from_env()
    checkpoint = CheckpointStore.from_env()
    await database.initialize()
    await checkpoint.initialize()
    data = AuthTestData(database, checkpoint)
    users = Users(database)
    prefix = "auth-accept-" + uuid.uuid4().hex
    try:
        for index, source in enumerate(("user_002", "user_001")):
            identity = f"{prefix}-{index}"
            password = secrets.token_urlsafe(24)
            account = await users.create(identity, hash_password(password), identity)
            data.accounts.append(account)
            data.passwords.append(password)
            with repository.transaction() as connection:
                connection.execute("INSERT INTO users (user_id,display_name,created_at) "
                                   "SELECT ?,display_name,created_at FROM users WHERE user_id=?", (identity, source))
                connection.execute("UPDATE orders SET user_id=? WHERE user_id=?", (identity, source))
        yield data
    finally:
        # Only rows belonging to accounts created by this invocation are removed.
        for account in data.accounts:
            for session in await Sessions(database).list_owned(account["id"]):
                sid = session["session_id"]
                async with checkpoint.session_lock(sid):
                    cp = await checkpoint.load(sid, account["business_user_id"])
                    if cp:
                        await checkpoint.delete(sid, cp.user_id, cp.version)
                await Sessions(database).delete(sid, account["id"])
            await database._call(lambda _c, cursor, account=account: cursor.execute(
                "DELETE FROM platform_user WHERE id=%s AND username=%s AND business_user_id=%s",
                (account["id"], account["username"], account["business_user_id"])))
