"""Provision a local login account for the Personal Copilot workbench.

Password comes from getpass or SMARTCS_DEMO_PASSWORD, never CLI arguments/output.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from dotenv import load_dotenv

from auth.password import hash_password
from platform_db import PlatformConflict, PlatformDatabase, PlatformUnavailable, Users


async def provision(username: str, business_user_id: str, password: str) -> dict:
    password_hash = await asyncio.to_thread(hash_password, password)
    db = PlatformDatabase.from_env()
    await db.initialize()
    return await Users(db).create(username, password_hash, business_user_id)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Create one login account in platform_user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--business-user-id", default=None)
    args = parser.parse_args()
    business_user_id = (args.business_user_id or args.username).strip()
    try:
        password = os.getenv("SMARTCS_DEMO_PASSWORD")
        if password is None:
            if not sys.stdin.isatty():
                raise ValueError("use a secure interactive terminal or set SMARTCS_DEMO_PASSWORD for local setup")
            password = getpass.getpass("Password (8-1024 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("password confirmation does not match")
        user = asyncio.run(provision(args.username, business_user_id, password))
    except (ValueError, PlatformConflict, PlatformUnavailable) as exc:
        parser.exit(1, f"Account was not created: {exc}\n")
    print(f"Created platform account id={user['id']}; password is not displayed.")


if __name__ == "__main__":
    main()
