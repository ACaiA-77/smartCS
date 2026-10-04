"""Phase 7 P7-2/P7-6, end to end across both services.

Nothing in the request path is substituted:

    api.main.chat (real handler)
      -> internal_api.harness_client.forward_chat (real HTTP)
        -> pi-harness /api/chat (real server process, real receipt log)
          -> POST /internal/auth/verify on the real Python internal router
            -> the Faux model (the only substitution, and it is offline)

What is asserted lives in an authority, not in the harness's own account of
itself: the MySQL conversation_session / agent_run_receipt / memory_source_event
rows decide whether a turn really ran once, and the tripwire orchestrator
decides that no pi turn ever reached the legacy path.

Skipped when node or the harness's tsx runner is missing; the rest of the suite
stays runnable without them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import HTTPException

import api.main as main
from auth.context import UserContext
from auth.jwt import issue_token
from platform_db.sessions import Sessions
from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    connect,
    platform_database,
    random_username,
    seed_account,
)

PYTHON_IMPL = Path(__file__).resolve().parents[1]
PI_HARNESS = PYTHON_IMPL.parent / "pi-harness"
TSX_CLI = PI_HARNESS / "node_modules" / "tsx" / "dist" / "cli.mjs"
MATRIX_HARNESS = PI_HARNESS / "tests" / "fixtures" / "matrix-harness.ts"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not TSX_CLI.exists(),
    reason="cross-service end-to-end needs node and pi-harness/node_modules/tsx",
)

TURN_TEXT = "我的订单发货了吗？"
HARNESS_ANSWER = "E2E 回答：订单已发货。"


class _TripwireOrchestrator:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, _state):
        self.calls += 1
        raise AssertionError("legacy orchestrator was invoked for a pi session")


class _ThreadedServer:
    """A real uvicorn server for the real internal router, on a background thread."""

    def __init__(self, app, port: int):
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.1)
        raise RuntimeError("python internal api did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Harness:
    def __init__(self, child: subprocess.Popen, port: int):
        self.child = child
        self.port = port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def kill(self) -> None:
        if self.child.poll() is None:
            self.child.kill()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _start_harness(python_url: str, port: int, script_file: Path, root: Path, secrets: dict) -> _Harness:
    env = {
        **os.environ,
        # Same database and secrets as the runtime under test, handed over
        # explicitly: the child starts from a raw environment.
        "MYSQL_DATABASE": TEST_DATABASE,
        "MATRIX_HARNESS_PORT": str(port),
        "MATRIX_SCRIPT_FILE": str(script_file),
        "MATRIX_SCRIPT_REPEAT": "2",
        "MATRIX_RUNTIME_CWD": str(root / "cwd"),
        "MATRIX_SESSION_DIR": str(root / "sessions"),
        "MATRIX_AGENT_DIR": str(root / "agent"),
        "PYTHON_INTERNAL_BASE_URL": python_url,
        # Phase 2+ needs a durable receipt log; the Faux provider keeps it offline.
        "SMARTCS_WRITE_MODE": "off",
        **secrets,
    }
    child = subprocess.Popen(
        [shutil.which("node"), str(TSX_CLI), str(MATRIX_HARNESS)],
        cwd=str(PI_HARNESS),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    harness = _Harness(child, port)
    deadline = time.time() + 90
    while time.time() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"harness exited during startup: {child.stderr.read() if child.stderr else ''}")
        try:
            if httpx.get(f"{harness.url}/health", timeout=1.0).status_code == 200:
                return harness
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    harness.kill()
    raise RuntimeError("harness did not start within 90s")


def _rows(sql: str, params: tuple) -> list[tuple]:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        connection.close()


def _receipts(session_id: str) -> list[dict]:
    rows = _rows("SELECT client_request_id, status, response FROM agent_run_receipt WHERE session_id=%s", (session_id,))
    return [{"client_request_id": r[0], "status": r[1], "response": json.loads(r[2]) if r[2] else None} for r in rows]


def _source_events(session_id: str) -> list[str]:
    return [r[0] for r in _rows(
        "SELECT client_request_id FROM memory_source_event WHERE session_id=%s ORDER BY created_at", (session_id,))]


@pytest.fixture
def secrets(monkeypatch):
    """One shared credential set for the runtime, the internal router and the
    harness — read from the same environment by all three."""
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")
    return {"AUTH_JWT_SECRET": USER_SECRET, "INTERNAL_SERVICE_JWT_SECRET": SERVICE_SECRET}


@pytest.mark.asyncio
async def test_a_new_session_goes_through_the_unified_entry_into_the_harness(
    monkeypatch, tmp_path, secrets
):
    """P7-2 with the real harness: bucket -> create(pi) -> forward -> receipt."""
    apply_migration()
    business_user_id = random_username("bu-e2e")
    account_id = seed_account(random_username("e2e"), business_user_id)
    user = UserContext(account_id=account_id, username="e2e", business_user_id=business_user_id)
    token = issue_token(account_id)

    python_port, harness_port = _free_port(), _free_port()
    monkeypatch.setenv("PI_HARNESS_BASE_URL", f"http://127.0.0.1:{harness_port}")

    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps([{"kind": "text", "text": HARNESS_ANSWER}]), encoding="utf-8")

    app = await build_app()
    internal = _ThreadedServer(app, python_port)
    internal.start()
    harness = _start_harness(f"http://127.0.0.1:{python_port}", harness_port, script_file,
                            tmp_path, secrets)
    try:
        database = platform_database()
        await database.initialize()
        sessions = Sessions(database)
        monkeypatch.setattr(main.app.state, "platform_sessions", sessions, raising=False)
        tripwire = _TripwireOrchestrator()
        monkeypatch.setattr(main, "chat_orchestrator", tripwire)

        first = await main.chat(
            main.ChatRequest(message=TURN_TEXT, client_request_id="e2e-turn-1"),
            user, token)

        # (a) the answer came back through the harness and was adapted
        assert first.response == HARNESS_ANSWER
        assert first.harness_version == "pi"
        assert first.intent == "order"            # the harness's own label
        assert first.compliance_passed is True
        assert first.client_request_id == "e2e-turn-1"

        # (b) the session was pinned to pi at creation
        stored = await sessions.get_owned(first.session_id, account_id)
        assert stored["harness_version"] == "pi"

        # (c) the harness's durable log shows exactly one completed run
        assert [(r["client_request_id"], r["status"]) for r in _receipts(first.session_id)] == [
            ("e2e-turn-1", "completed")]
        assert _source_events(first.session_id) == ["e2e-turn-1"]
        assert tripwire.calls == 0

        # (d) 回执幂等: the same request replays without a second model run
        replay = await main.chat(
            main.ChatRequest(message=TURN_TEXT, session_id=first.session_id, client_request_id="e2e-turn-1"),
            user, token)
        assert replay.response == HARNESS_ANSWER
        assert replay.session_id == first.session_id
        assert _source_events(first.session_id) == ["e2e-turn-1"]

        # (e) P7-6: harness down -> 503, never a legacy turn
        harness.kill()
        with pytest.raises(HTTPException) as excinfo:
            await main.chat(
                main.ChatRequest(message=TURN_TEXT, session_id=first.session_id, client_request_id="e2e-outage"),
                user, token)
        assert excinfo.value.status_code == 503
        assert tripwire.calls == 0
        assert [r["client_request_id"] for r in _receipts(first.session_id)] == ["e2e-turn-1"]

        # (f) P7-6 recovery: the same request, retried, now succeeds
        restarted = _start_harness(f"http://127.0.0.1:{python_port}", harness_port, script_file,
                                   tmp_path, secrets)
        try:
            recovered = await main.chat(
                main.ChatRequest(message=TURN_TEXT, session_id=first.session_id, client_request_id="e2e-outage"),
                user, token)
            assert recovered.response == HARNESS_ANSWER
            assert recovered.session_id == first.session_id
            assert recovered.harness_version == "pi"
            assert tripwire.calls == 0
            assert {r["client_request_id"] for r in _receipts(first.session_id)} == {"e2e-turn-1", "e2e-outage"}
        finally:
            restarted.kill()
    finally:
        harness.kill()
        internal.stop()
