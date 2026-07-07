from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class AgentApiError(RuntimeError):
    """Raised when the SmartCS HTTP API cannot complete a request."""


@dataclass(frozen=True)
class ChatResult:
    response: str
    session_id: str
    intent: str
    compliance_passed: bool


class AgentApiClient:
    """Small HTTP client that talks to the existing FastAPI endpoints."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def health(self) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/health")
        return self._json_or_raise(response)

    def chat(self, message: str, user_id: str, session_id: str) -> ChatResult:
        payload = {
            "message": message,
            "user_id": user_id,
            "session_id": session_id,
        }
        response = self._client.post(f"{self.base_url}/api/chat", json=payload)
        data = self._json_or_raise(response)
        return ChatResult(
            response=str(data.get("response", "")),
            session_id=str(data.get("session_id", session_id)),
            intent=str(data.get("intent", "")),
            compliance_passed=bool(data.get("compliance_passed", False)),
        )

    def history(self, session_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/api/history/{session_id}")
        return self._json_or_raise(response)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise AgentApiError("API returned non-JSON response") from exc

        detail = response.text
        try:
            payload = response.json()
            detail = str(payload.get("detail", payload))
        except ValueError:
            pass
        raise AgentApiError(f"HTTP {response.status_code}: {detail}")
