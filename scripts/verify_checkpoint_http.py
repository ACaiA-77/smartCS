"""Authenticated HTTP restart acceptance: real JWT/MySQL, isolated SQLite, fake LLM.

Optional browser checks login, own orders, refresh, lost POST and account switch.
Only UUID test accounts and their sessions are cleaned.
"""
from __future__ import annotations

import argparse
import asyncio
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def browser_acceptance(base, data, repository, alice):
    from playwright.sync_api import sync_playwright

    screenshots = Path(__file__).resolve().parents[1] / "artifacts" / "auth_20260919"
    screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            page.goto(base)
            page.wait_for_function("!document.querySelector('#login-button').disabled")
            assert page.locator("#app-shell").is_hidden()
            page.screenshot(path=str(screenshots / "login.png"), full_page=True)

            def login(index):
                credentials = data.credentials(index)
                page.locator("#username").fill(credentials["username"])
                page.locator("#password").fill(credentials["password"])
                page.locator("#login-button").click()
                page.wait_for_function("state.user !== null && !state.sending && document.querySelectorAll('[data-order-id]').length > 0")
                assert page.locator("#account-name").inner_text() == credentials["username"]
                assert page.locator("#password").input_value() == ""
                orders = page.locator("[data-order-id]").evaluate_all("nodes => nodes.map(node => node.dataset.orderId)")
                assert orders
                assert all(repository.get_order(oid)["user_id"] == data.accounts[index]["business_user_id"] for oid in orders)
                return orders

            orders_a = login(0)
            page.locator("#new-session").click()
            page.locator("#message-input").fill("帮我退款 ORD-20260801-0022")
            page.locator("#send-button").click()
            page.wait_for_function("!state.sending && state.pending === null && state.sessionId !== null")
            sid = page.evaluate("state.sessionId")
            page.reload()
            page.wait_for_function("state.user !== null && !state.sending && document.querySelectorAll('#message-list .message').length === 2")
            assert page.evaluate("state.sessionId") == sid
            assert len(alice(f"/api/history/{sid}")["messages"]) == 2
            assert alice(f"/api/checkpoints/{sid}")["current_stage"] == "WAIT_CONFIRM"
            assert repository.get_order("ORD-20260801-0022")["refunds"] == []
            page.screenshot(path=str(screenshots / "customer.png"), full_page=True)

            # Fail POST before arrival. Explicit Continue must reuse the request ID.
            page.route("**/api/chat", lambda route: route.abort(), times=1)
            page.locator("#message-input").fill("取消退款")
            page.locator("#send-button").click()
            page.locator("[data-resume]").wait_for()
            page.wait_for_function("!state.sending")
            rid = page.evaluate("state.pending.client_request_id")
            page.locator("[data-resume]").click()
            page.wait_for_function("!state.sending && state.pending === null")
            assert len(alice(f"/api/history/{sid}")["messages"]) == 4
            assert alice(f"/api/checkpoints/{sid}")["client_request_id"] == rid
            assert repository.get_order("ORD-20260801-0022")["refunds"] == []

            page.locator("#logout-button").click()
            page.wait_for_function("state.user === null && !document.querySelector('#login-button').disabled")
            assert page.locator("#app-shell").is_hidden()
            assert page.locator("#message-list").inner_text() == ""
            assert page.locator("[data-session-id]").count() == 0
            assert page.locator("[data-order-id]").count() == 0
            orders_b = login(1)
            assert set(orders_a).isdisjoint(orders_b)
            assert page.locator(f'[data-session-id="{sid}"]').count() == 0
            assert page.locator("#message-list").inner_text() == ""
            denied = page.request.get(f"{base}/api/history/{sid}")
            assert denied.status == 404
            assert denied.json() == {"detail": "session not found"}
            page.reload()
            page.wait_for_function("state.user !== null && !state.sending")
            assert page.locator(f'[data-session-id="{sid}"]').count() == 0
            assert page.locator("#message-list").inner_text() == ""
            return {"login_own_orders": True, "refresh_history": True, "wait_confirm_refresh": True, "lost_post": True,
                    "logout_clears_data": True, "account_switch_isolated": True}
        finally:
            browser.close()


async def verify(args):
    from mcp.order_repository import OrderRepository
    from tests.auth_integration_helpers import auth_test_data

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def client():
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        def request(path, body=None, method=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Origin": base}, method=method)
            with opener.open(req, timeout=30) as response:
                return json.load(response)
        return request

    alice, bob, anonymous = client(), client(), client()
    with tempfile.TemporaryDirectory(prefix="smartcs-http-checkpoint-") as directory:
        repository = OrderRepository(str(Path(directory) / "orders.db"))
        env = {**os.environ, "ORDER_DB_PATH": str(repository.db_path),
               "REDIS_URL": "redis://127.0.0.1:6399/0", "FAISS_INDEX_PATH": str(Path(directory) / "no-index"),
               "AUTH_JWT_SECRET": secrets.token_urlsafe(48), "AUTH_COOKIE_SECURE": "false", "CORS_ALLOWED_ORIGINS": ""}
        process = None

        def start():
            proc = subprocess.Popen([sys.executable, "-m", "scripts.verify_checkpoint_http", "--serve", "--port", str(port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError("acceptance API failed to start: " + proc.stderr.read().decode(errors="replace"))
                try:
                    anonymous("/health")
                    return proc
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError("acceptance API startup timeout")

        async with auth_test_data(repository) as data:
            try:
                process = start()
                alice("/api/auth/login", data.credentials(0))
                bob("/api/auth/login", data.credentials(1))
                prepare = {"client_request_id": "prepare-http", "message": "帮我退款 ORD-20260801-0002"}
                first = alice("/api/chat", prepare)
                session = first["session_id"]
                assert "确认退款" in first["response"]
                assert alice("/api/checkpoints/" + session)["current_stage"] == "WAIT_CONFIRM"
                old_pid = process.pid
                process.kill()
                process.wait(timeout=10)
                process = start()
                # Explicit re-login proves durable account/session ownership.
                alice("/api/auth/login", data.credentials(0))
                history = alice(f"/api/history/{session}")
                assert len(history["messages"]) == 2
                pending = alice(f"/api/checkpoints/{session}/resume", {"client_request_id": "prepare-http"})
                assert "确认退款" in pending["response"]
                assert repository.get_order("ORD-20260801-0002")["refunds"] == []
                confirm = {"session_id": session, "client_request_id": "confirm-http", "message": "确认退款"}
                completed = alice("/api/chat", confirm)
                assert "退款申请已提交" in completed["response"]
                replay = alice("/api/chat", confirm)
                assert replay == completed
                assert len(alice(f"/api/history/{session}")["messages"]) == 4
                assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1
                for method, path, body in (("GET", f"/api/checkpoints/{session}", None),
                                            ("POST", f"/api/checkpoints/{session}/resume", {}),
                                            ("DELETE", f"/api/sessions/{session}", None)):
                    try:
                        bob(path, body, method)
                        raise AssertionError("cross-user operation succeeded")
                    except urllib.error.HTTPError as exc:
                        assert exc.code == 404
                alice(f"/api/history/{session}", method="DELETE")
                assert alice(f"/api/history/{session}")["messages"] == []
                browser = await asyncio.to_thread(browser_acceptance, base, data, repository, alice) if args.browser else None
                print(json.dumps({"passed": True, "first_pid": old_pid, "restarted_pid": process.pid,
                                  "history_restored": 2, "history_after_confirm_and_replay": 4,
                                  "cross_user_read": 404, "cross_user_resume_delete": 404,
                                  "refund_count": 1, "wait_confirm_not_autoapproved": True,
                                  "authentication": "real Argon2 login and HttpOnly JWT cookie",
                                  "llm": "deterministic test double", "database": "real MySQL",
                                  "business_database": "temporary isolated SQLite", "redis": "unavailable test port",
                                  "browser_reload_and_lost_post": args.browser, "browser_auth": browser}, indent=2))
            except BaseException:
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process and process.stderr:
                    print(process.stderr.read().decode("utf-8", errors="replace"), file=sys.stderr)
                raise
            finally:
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process and process.stderr:
                    process.stderr.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    os.environ.update(OTEL_SDK_DISABLED="true", EMBEDDING_BACKEND="hash", PYTHONIOENCODING="utf-8")
    if args.serve:
        import api.main as api
        from tests.conftest import MockLLM
        import uvicorn
        factory = api.create_chat_orchestrator
        api.create_chat_orchestrator = lambda **kwargs: factory(llm=MockLLM(), **kwargs)
        uvicorn.run(api.app, host="127.0.0.1", port=args.port, log_level="error")
        return
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
