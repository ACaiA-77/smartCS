"""Short-lived HS256 access tokens; logout clears the cookie, not copied tokens.

No refresh or revocation store: a copied token lasts until expiry (at most 30 min).
Account disablement takes effect on the next request through the database lookup.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

import jwt as pyjwt


ALGORITHM = "HS256"
COOKIE_NAME = "smartcs_auth"
COOKIE_PATH = "/"
TOKEN_TTL_SECONDS = 1800
ISSUER = "smartcs"


@dataclass(frozen=True)
class AuthSettings:
    secret: str = field(repr=False)
    issuer: str = ISSUER
    ttl_seconds: int = TOKEN_TTL_SECONDS
    cookie_secure: bool = False


def get_settings() -> AuthSettings:
    secret = os.getenv("AUTH_JWT_SECRET", "")
    if len(secret.encode("utf-8")) < 32 or secret != secret.strip():
        raise ValueError("AUTH_JWT_SECRET must contain at least 32 bytes without surrounding whitespace")
    secure = os.getenv("AUTH_COOKIE_SECURE", "false").lower()
    if secure not in {"true", "false"}:
        raise ValueError("AUTH_COOKIE_SECURE must be true or false")
    return AuthSettings(secret=secret, cookie_secure=secure == "true")


def cookie_options() -> dict:
    """Shared set_cookie/delete_cookie options; set_cookie also needs max_age."""
    return {"httponly": True, "secure": get_settings().cookie_secure,
            "samesite": "strict", "path": COOKIE_PATH}


def issue_token(account_id: int) -> str:
    if type(account_id) is not int or not 0 < account_id <= 2**63 - 1:
        raise ValueError("invalid account_id")
    settings = get_settings()
    now = int(time.time())
    return pyjwt.encode({"sub": str(account_id), "iat": now, "exp": now + settings.ttl_seconds,
                         "iss": settings.issuer, "jti": uuid.uuid4().hex},
                        settings.secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise pyjwt.InvalidTokenError("invalid access token")
    claims = pyjwt.decode(token, settings.secret, algorithms=[ALGORITHM], issuer=settings.issuer,
                         options={"require": ["sub", "iat", "exp", "iss", "jti"]})
    sub = claims["sub"]
    if (not isinstance(sub, str) or not sub.isascii() or not sub.isdigit()
            or not 0 < int(sub) <= 2**63 - 1 or str(int(sub)) != sub
            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= settings.ttl_seconds
            or not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 128):
        raise pyjwt.InvalidTokenError("invalid access token claims")
    return claims
