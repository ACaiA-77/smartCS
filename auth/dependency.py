"""JWT verification -> active MySQL account -> request-lifetime UserContext."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from jwt import InvalidTokenError

from auth.context import UserContext, current_user
from auth.jwt import COOKIE_NAME, decode_token
from platform_db.database import PlatformUnavailable


def _origin(value: str, *, referer: bool = False) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or (not referer and (parsed.path not in {"", "/"} or parsed.query))
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value or "*" in value):
        raise ValueError("invalid origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    suffix = "" if port == (443 if parsed.scheme == "https" else 80) else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def cors_allowed_origins() -> list[str]:
    """No wildcard, regular expression, implicit subdomains or credentialed any-origin."""
    return list(dict.fromkeys(_origin(item.strip()) for item in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if item.strip()))


def check_request_origin(request: Request) -> None:
    """Cookie writes and login require an exact Origin or same-origin Referer.

    Call on login even before a cookie exists. Pure Bearer requests do not call
    this helper in get_current_user. CLI login can explicitly send its API Origin.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    allowed = {str(request.base_url).rstrip("/"), *cors_allowed_origins()}
    try:
        origin = request.headers.get("origin")
        if origin is not None:
            source = _origin(origin)
        else:
            source = _origin(request.headers.get("referer", ""), referer=True)
        if source not in {_origin(value) for value in allowed}:
            raise ValueError("untrusted origin")
    except ValueError:
        raise HTTPException(status_code=403, detail="request origin not allowed") from None


async def get_current_user(request: Request) -> AsyncIterator[UserContext]:
    """Never take identity from request bodies, query strings or JWT business fields."""
    if any(name in request.query_params for name in ("user_id", "business_user_id")):
        raise HTTPException(status_code=400, detail="identity must come from authentication")
    cookie = request.cookies.get(COOKIE_NAME)
    authorization = request.headers.get("authorization")
    token = cookie
    if authorization is not None:
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or (cookie and cookie != parts[1]):
            raise HTTPException(status_code=401, detail="invalid authentication")
        token = parts[1]
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        claims = decode_token(token)
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid or expired authentication") from None
    except ValueError:
        raise HTTPException(status_code=503, detail="authentication not configured") from None
    users = getattr(request.app.state, "platform_users", None)
    if users is None:
        raise HTTPException(status_code=503, detail="authentication unavailable")
    try:
        account = await users.by_id(int(claims["sub"]))
    except PlatformUnavailable:
        raise HTTPException(status_code=503, detail="authentication unavailable") from None
    if (not account or account.get("status") != "active" or account.get("id") != int(claims["sub"])
            or not isinstance(account.get("username"), str) or not account["username"]
            or not isinstance(account.get("business_user_id"), str) or not account["business_user_id"]):
        raise HTTPException(status_code=401, detail="invalid or inactive account")
    if cookie:
        check_request_origin(request)
    user = UserContext(account_id=account["id"], username=account["username"],
                       business_user_id=account["business_user_id"])
    context_token = current_user.set(user)
    try:
        yield user
    finally:
        current_user.reset(context_token)
