"""Deterministic patching of ``app.state`` attributes.

Starlette's ``State`` keeps attributes in a dict and raises ``AttributeError``
when one is missing. ``monkeypatch.setattr(..., raising=False)`` therefore
records "absent" and undoes the patch with ``delattr``. Two such patches on the
same name — or a later plain ``setattr`` in the same test — then race over that
delete, and the suite fails at teardown with ``KeyError`` depending on test
order. Binding the previous value to ``None`` first turns every undo into a
restore.
"""

from __future__ import annotations

from typing import Any


def patch_app_state(monkeypatch, app, /, **values: Any) -> None:
    for name, value in values.items():
        if not hasattr(app.state, name):
            print(f"DIAG-ABSENT {name} app={id(app)} state={id(app.state)}", flush=True)
            monkeypatch.setattr(app.state, name, None, raising=False)
        monkeypatch.setattr(app.state, name, value)
