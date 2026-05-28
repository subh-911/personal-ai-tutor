from __future__ import annotations

from uuid import UUID

from fastapi import Header


# Stable fallback used when no Authorization header is present. Acts as a single
# bucket for anonymous traffic until real auth (JWT/OAuth) lands. Anything that
# *does* arrive with a Bearer header gets its own per-user Redis namespace.
ANONYMOUS_USER_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


async def get_user_id(authorization: str | None = Header(default=None)) -> UUID:
    """Resolve the calling user id.

    Reads `Authorization: Bearer <uuid>`. Falls back to `ANONYMOUS_USER_ID` if the
    header is missing, the scheme isn't `Bearer`, or the token isn't a valid UUID.
    This is intentionally permissive — it's session scoping, not access control.
    Real authentication slots in by swapping this dependency for one that verifies
    a JWT and returns a `users.id` UUID.
    """
    if not authorization:
        return ANONYMOUS_USER_ID
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ANONYMOUS_USER_ID
    try:
        return UUID(value.strip())
    except ValueError:
        return ANONYMOUS_USER_ID
