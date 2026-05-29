"""Phase 8 — JWT-verifying user-id dependency.

The default code path (`settings.dev_auth_bypass=False`, the production setting)
requires `Authorization: Bearer <jwt>` on every request consuming `Depends(get_user_id)`.
The JWT is verified against Clerk's published JWKS (`settings.clerk_jwks_url`)
with `iss == settings.clerk_issuer`, and the `sub` claim is returned as the
verified user id (a Clerk user id like `user_2abc123def`).

The escape hatch (`settings.dev_auth_bypass=True`) re-enables the pre-phase-8
permissive parser: any Bearer value is returned verbatim as the user id, with a
fallback to a fixed anonymous string when the header is missing. Intended for
ad-hoc `curl` smoke testing only. Logs a startup warning so it can't be enabled
silently.
"""
from __future__ import annotations

import logging

from fastapi import Header, HTTPException, status
from jwt import (
    InvalidTokenError,
    PyJWKClient,
    PyJWKClientError,
    decode as jwt_decode,
)

from app.config import settings

log = logging.getLogger(__name__)


# Used only in dev-bypass mode when the request has no Authorization header.
ANONYMOUS_USER_ID = "anon_dev_user"


# PyJWKClient caches signing keys for its lifetime. We lazy-create a single
# module-level instance on first use so the JWKS HTTP fetch happens at request
# time, not at import time (keeps tests / `alembic` invocations from making
# outbound calls just to load the module).
_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not settings.clerk_jwks_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CLERK_JWKS_URL is not configured",
            )
        _jwk_client = PyJWKClient(settings.clerk_jwks_url)
    return _jwk_client


if settings.dev_auth_bypass:
    log.warning(
        "DEV_AUTH_BYPASS is ENABLED — auth verification is OFF. "
        "Any Bearer token will be accepted verbatim as the user id. "
        "Never enable this in production."
    )


def _credential_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_user_id(authorization: str | None = Header(default=None)) -> str:
    """Resolve the verified calling user id from an Authorization header.

    Returns the JWT `sub` claim (a Clerk user id string) when bypass is off;
    returns the raw Bearer value (or `ANONYMOUS_USER_ID` if absent) when bypass
    is on. Raises 401 if verification fails.
    """
    if settings.dev_auth_bypass:
        if not authorization:
            return ANONYMOUS_USER_ID
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return ANONYMOUS_USER_ID
        return value.strip()

    if not authorization:
        raise _credential_error()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _credential_error()
    token = token.strip()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        payload = jwt_decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={"require": ["sub", "iss", "exp"]},
        )
    except (InvalidTokenError, PyJWKClientError) as exc:
        log.info("rejected JWT: %s", exc)
        raise _credential_error() from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _credential_error()
    return sub
