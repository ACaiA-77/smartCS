"""Phase 11 §4.2 acceptance: `GET /internal/ready`.

The harness's `/ready` asks this endpoint "can the Business Runtime serve a
turn?". The contract these cases pin:

    R1  every dependency wired and reachable      -> 200, ok true
    R2  no service credential                     -> 401
    R3  the WRONG direction of service token      -> 401
    R4  platform database unreachable             -> 503, platform_db false
    R5  tool runtime not wired                    -> 503, tool_runtime false
    R6  memory runtime not initialized            -> 503, memory_runtime false
    R7  the answer never leaks an internal detail

Real router, real MySQL test database, real SQLite order store — nothing is
mocked. No model, RAG or business tool is exercised: readiness must be cheap by
contract, and R8 asserts exactly that.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    build_order_repository,
    build_user_memory_service,
    platform_database,
)

READY_URL = "/internal/ready"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)


def ops_token(*, issuer: str = "smartcs-pi-harness", audience: str = "smartcs-business-runtime", ttl: int = 30) -> str:
    """The turn-less service token the harness mints for a readiness call."""
    now = int(time.time())
    return pyjwt.encode(
        {"iss": issuer, "aud": audience, "iat": now, "exp": now + ttl},
        SERVICE_SECRET,
        algorithm="HS256",
    )


def ops_header(**kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {ops_token(**kwargs)}"}


@pytest_asyncio.fixture
async def ready_app(tmp_path):
    apply_migration()
    repository = build_order_repository(tmp_path)
    app = await build_app(
        tool_executor=object(),
        order_repository=repository,
        user_memory_service=await build_user_memory_service(),
    )
    app.state.platform_database = platform_database()
    return app


async def request(app, headers: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(READY_URL, headers=headers or {})


@pytest.mark.asyncio
async def test_r1_all_dependencies_up_is_ready(ready_app):
    response = await request(ready_app, ops_header())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "platform_db": True,
        "tool_runtime": True,
        "memory_runtime": True,
    }


@pytest.mark.asyncio
async def test_r2_no_credential_is_refused(ready_app):
    response = await request(ready_app)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_r3_the_opposite_service_direction_is_refused(ready_app):
    """A runtime->harness token must not open a harness->runtime endpoint."""
    response = await request(
        ready_app,
        ops_header(issuer="smartcs-business-runtime", audience="smartcs-pi-harness"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_r4_platform_database_unreachable(ready_app, monkeypatch):
    from platform_db.database import PlatformDatabase

    # A port nothing listens on: reachability, not configuration, is what fails.
    ready_app.state.platform_database = PlatformDatabase(
        host="127.0.0.1", port=1, database=TEST_DATABASE, user="nobody", password="nobody"
    )
    response = await request(ready_app, ops_header())
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["platform_db"] is False


@pytest.mark.asyncio
async def test_r5_tool_runtime_not_wired(ready_app):
    ready_app.state.order_repository = None
    response = await request(ready_app, ops_header())
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["tool_runtime"] is False


@pytest.mark.asyncio
async def test_r6_memory_runtime_not_initialized(ready_app):
    ready_app.state.user_memory_service = None
    response = await request(ready_app, ops_header())
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["memory_runtime"] is False


@pytest.mark.asyncio
async def test_r7_the_answer_carries_no_internal_detail(ready_app):
    ready_app.state.platform_database = None
    response = await request(ready_app, ops_header())
    payload = response.text
    for leak in ("mysql", "pymysql", "Traceback", TEST_DATABASE, "password", "secret", "127.0.0.1"):
        assert leak.lower() not in payload.lower(), leak


@pytest.mark.asyncio
async def test_r8_readiness_does_no_business_work(ready_app, monkeypatch):
    """Readiness must not run an LLM, a retrieval or a business tool."""

    def explode(*_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("readiness executed business work")

    # The tool executor is a bare object here; if readiness tried to call it,
    # the probe would fail. Assert the probe still answers.
    ready_app.state.tool_executor = explode
    response = await request(ready_app, ops_header())
    assert response.status_code == 200, response.text
