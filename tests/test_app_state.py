"""The helper must never leave a deletion for teardown to fail on."""

from __future__ import annotations

from types import SimpleNamespace

from _pytest.monkeypatch import MonkeyPatch
from starlette.datastructures import State

from tests.app_state import patch_app_state


def test_patch_on_an_undeclared_handle_restores_instead_of_deleting():
    app = SimpleNamespace(state=State())
    monkeypatch = MonkeyPatch()
    patch_app_state(monkeypatch, app, platform_sessions="patched")
    assert app.state.platform_sessions == "patched"

    monkeypatch.undo()  # must not raise KeyError

    assert "platform_sessions" in app.state._state
    assert app.state.platform_sessions is None


def test_patch_restores_the_previous_value():
    app = SimpleNamespace(state=State())
    app.state.platform_sessions = "original"
    monkeypatch = MonkeyPatch()
    patch_app_state(monkeypatch, app, platform_sessions="patched")
    monkeypatch.undo()
    assert app.state.platform_sessions == "original"
