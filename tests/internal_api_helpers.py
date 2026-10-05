"""Shared fixtures for the /internal/* acceptance tests.

Phase 1 put these tests outside python-impl/tests/ because the write whitelist
did not include it; Phase 2 moved them in (D8 裁决). Everything here talks to
the REAL router and the REAL MySQL test database — nothing is mocked except the
model, which the Python side never touches anyway.

The MySQL test database (`smartcs_phase1_test`) is isolated from the running
service's database and is dropped/recreated per test module.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import jwt as pyjwt
import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI

PYTHON_IMPL = Path(__file__).resolve().parents[1]
if str(PYTHON_IMPL) not in sys.path:
    sys.path.insert(0, str(PYTHON_IMPL))

load_dotenv(PYTHON_IMPL / ".env")

TEST_DATABASE = os.getenv("SMARTCS_TEST_DATABASE", "smartcs_phase1_test")
MIGRATIONS = [
    PYTHON_IMPL / "migrations" / "001_phase1_session_foundation.sql",
    PYTHON_IMPL / "migrations" / "002_phase3_memory_outbox.sql",
    PYTHON_IMPL / "migrations" / "003_phase5_write_enable.sql",
    # Phase 6: the audit sink. Additive and idempotent, so the existing suites
    # are unaffected; the Phase 5 tests that need pending_action now get it from
    # this list instead of depending on a previous run having created it.
    PYTHON_IMPL / "migrations" / "004_phase6_audit.sql",
]

#: Test-only credentials. Never the production values; injected per-test via
#: monkeypatch so nothing leaks into the rest of the suite.
USER_SECRET = "internal-api-test-user-secret-0123456789"
SERVICE_SECRET = "internal-api-test-service-secret-0123456789"


def connect(with_db: bool = True):
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3307")),
        user=os.getenv("MYSQL_USER", "smartcs"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=TEST_DATABASE if with_db else None,
        charset="utf8mb4",
        autocommit=True,
    )


def apply_migration() -> None:
    """Drop and recreate the Phase 1 tables using the real migration script."""
    connection = connect()
    try:
        with connection.cursor() as cursor:
            for table in (
                "audit_event",
                "memory_source_event",
                "agent_run_receipt",
                "conversation_session",
                "platform_user",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
            cursor.execute("""CREATE TABLE platform_user (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                password_hash VARCHAR(512) NOT NULL,
                business_user_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE conversation_session (
                session_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
                account_id BIGINT NOT NULL,
                title VARCHAR(200) NOT NULL DEFAULT '',
                client_request_id VARCHAR(128) COLLATE utf8mb4_bin NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY account_initial_request (account_id, client_request_id),
                CONSTRAINT session_account_fk FOREIGN KEY (account_id) REFERENCES platform_user(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            # PREPARE/EXECUTE are session-scoped, so each script must run on
            # this one connection, statement by statement.
            for migration in MIGRATIONS:
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    stripped = statement.strip()
                    if not stripped or all(
                        line.strip().startswith("--") or not line.strip() for line in stripped.splitlines()
                    ):
                        continue
                    cursor.execute(stripped)
    finally:
        connection.close()


def seed_account(username: str, business_user_id: str, status: str = "active") -> int:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO platform_user (username, password_hash, business_user_id, status) VALUES (%s,%s,%s,%s)",
                (username, "x", business_user_id, status),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def seed_session(session_id: str, account_id: int, harness_version: str = "pi") -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO conversation_session (session_id, account_id, title, harness_version) VALUES (%s,%s,'',%s)",
                (session_id, account_id, harness_version),
            )
    finally:
        connection.close()


def user_token(account_id: int, ttl: int = 1800, secret: str | None = None) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": str(account_id), "iat": now, "exp": now + ttl, "iss": "smartcs", "jti": uuid.uuid4().hex},
        secret or USER_SECRET,
        algorithm="HS256",
    )


def service_token(
    account_id: int,
    session_id: str,
    *,
    business_user_id: str | None = None,
    client_request_id: str = "req-1",
    ttl: int = 60,
    secret: str | None = None,
    audience: str = "smartcs-business-runtime",
    issuer: str = "smartcs-pi-harness",
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "account_id": account_id,
        "session_id": session_id,
        "client_request_id": client_request_id,
        "iat": now,
        "exp": now + ttl,
    }
    if business_user_id is not None:
        claims["business_user_id"] = business_user_id
    return pyjwt.encode(claims, secret or SERVICE_SECRET, algorithm="HS256")


def service_header(**kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {service_token(**kwargs)}"}


def platform_database(database: str | None = None):
    """`PlatformDatabase` PINNED to the test database (Phase 6b).

    `PlatformDatabase.from_env()` reads `MYSQL_DATABASE`, which defaults to the
    service database in `.env` — so a test module that forgot to monkeypatch it
    would silently talk to `smartcs_checkpoint` instead of the isolated test
    database. The test database name comes from the same variable both runners
    share (`SMARTCS_TEST_DATABASE`), so there is exactly one place to change it.
    """
    from platform_db.database import PlatformDatabase

    return PlatformDatabase(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3307")),
        database=database or TEST_DATABASE,
        user=os.getenv("MYSQL_USER", "smartcs"),
        password=os.getenv("MYSQL_PASSWORD", ""),
    )


async def build_app(
    *,
    tool_executor: Any = None,
    order_repository: Any = None,
    user_memory_service: Any = None,
) -> FastAPI:
    """A FastAPI app mounting the real internal router with real DB state."""
    from internal_api import internal_router
    from platform_db.sessions import Sessions
    from platform_db.users import Users

    database = platform_database()
    await database.initialize()
    app = FastAPI()
    app.include_router(internal_router)
    app.state.platform_users = Users(database)
    app.state.platform_sessions = Sessions(database)
    app.state.tool_executor = tool_executor
    app.state.order_repository = order_repository
    app.state.user_memory_service = user_memory_service
    return app


def build_order_repository(tmp_path: Path):
    """The real SQLite order repository, seeded with the demo dataset."""
    from mcp.order_repository import OrderRepository

    return OrderRepository(str(tmp_path / "orders.db"))


async def build_user_memory_service():
    """The real UserMemoryService over the (test) platform database."""
    from memory.user_memory import UserMemoryService
    from platform_db.database import PlatformDatabase

    service = UserMemoryService(database=PlatformDatabase.from_env())
    await service.initialize()
    return service


def seed_memory_source_event(session_id: str, business_user_id: str, client_request_id: str, content: str) -> str:
    """Insert the provenance row the outbox consumer reads back."""
    event_id = str(uuid.uuid4())
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO memory_source_event
                   (event_id, session_id, business_user_id, client_request_id, content)
                   VALUES (%s,%s,%s,%s,%s)""",
                (event_id, session_id, business_user_id, client_request_id, content),
            )
    finally:
        connection.close()
    return event_id


def build_tool_executor(tmp_path: Path):
    """The real MCP server + ToolExecutor stack over a seeded temp SQLite DB."""
    from mcp.mcp_server import MCPToolServer, create_default_tools
    from mcp.order_repository import OrderRepository
    from mcp.tool_execution import ToolExecutor
    from memory.long_term import LongTermMemory
    from refunds.service import RefundService
    from tickets.service import TicketService

    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(
        MCPToolServer(),
        long_term_memory=LongTermMemory(embedding_dim=64),
        order_repository=repository,
        refund_service=RefundService(repository),
        ticket_service=TicketService(repository),
    )
    return ToolExecutor(server), repository


def random_username(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"
