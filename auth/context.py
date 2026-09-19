"""Only verified backend account data may populate this request-local identity."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class UserContext:
    account_id: int
    username: str
    business_user_id: str


current_user: ContextVar[UserContext | None] = ContextVar("current_user", default=None)
