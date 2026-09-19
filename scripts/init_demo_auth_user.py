"""Link an operator-created login to an existing SQLite business user.

Password comes from getpass or SMARTCS_DEMO_PASSWORD, never CLI arguments/output.
This script never creates business users, resets passwords or updates existing links.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

from auth.password import hash_password
from platform_db import PlatformConflict, PlatformDatabase, PlatformUnavailable, Users


def verify_business_user(db_path: str, business_user_id: str) -> None:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise ValueError("business SQLite database does not exist; initialize it separately")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT 1 FROM users WHERE user_id=?", (business_user_id,)).fetchone()
    except sqlite3.Error:
        raise ValueError("cannot read the business user table") from None
    if row is None:
        raise ValueError("target business user does not exist")


async def provision(username: str, business_user_id: str, password: str, db_path: str) -> dict:
    verify_business_user(db_path, business_user_id)
    password_hash = await asyncio.to_thread(hash_password, password)
    db = PlatformDatabase.from_env()
    await db.initialize()
    return await Users(db).create(username, password_hash, business_user_id)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Create one login linked to an existing business user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--business-user-id", required=True)
    parser.add_argument("--db-path", default=os.getenv("ORDER_DB_PATH", "./data/orders.db"))
    args = parser.parse_args()
    try:
        verify_business_user(args.db_path, args.business_user_id)
        password = os.getenv("SMARTCS_DEMO_PASSWORD")
        if password is None:
            if not sys.stdin.isatty():
                raise ValueError("use a secure interactive terminal or set SMARTCS_DEMO_PASSWORD for local setup")
            password = getpass.getpass("Password (8-1024 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("password confirmation does not match")
        user = asyncio.run(provision(args.username, args.business_user_id, password, args.db_path))
    except (ValueError, PlatformConflict, PlatformUnavailable) as exc:
        parser.exit(1, f"Account was not created: {exc}\n")
    print(f"Created platform account id={user['id']}; business link verified. Password is not displayed.")


if __name__ == "__main__":
    main()
