"""Loopback URLs for tests that must not reach a real service."""

from __future__ import annotations

import socket


def unused_redis_url() -> str:
    """A loopback Redis URL whose port has no listener.

    A fixed port (6379, 6399, …) is not isolation: any developer machine or CI
    service may already own it, and the test then silently exercises the real
    backend instead of the fallback it is asserting.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"redis://127.0.0.1:{port}/0"
