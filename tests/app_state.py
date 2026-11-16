"""Deterministic patching of ``app.state`` attributes.

Starlette's ``State`` keeps attributes in a dict and raises ``AttributeError``
when one is missing. ``monkeypatch.setattr(..., raising=False)`` therefore
records "absent" and undoes the patch with ``delattr`` — and pytest's undo calls
that ``delattr`` without a guard, so a missing key fails the test at teardown.

Two ways this bites the suite: an app that never declared the handle, and any
later patch of a name whose earlier undo already removed it. Rather than depend
on that bookkeeping, record an explicit previous value (``None`` when the handle
did not exist) so every undo is a ``setattr`` and no test can delete a shared
handle another test is about to use.
"""

from __future__ import annotations

from typing import Any


def patch_app_state(monkeypatch, app, /, **values: Any) -> None:
    for name, value in values.items():
        previous = app.state._state.get(name)
        monkeypatch.setattr(app.state, name, value, raising=False)
        # Replace the undo entry with a restore: never NOTSET, never delattr.
        monkeypatch._setattr[-1] = (app.state, name, previous)
