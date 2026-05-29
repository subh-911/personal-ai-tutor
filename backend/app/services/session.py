from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis

from app.config import settings
from app.redis_client import redis as redis_client
from app.schemas.chat import ChatMessage


# Phase 6: keys are namespaced by user id so a leaked session UUID alone can't
# unlock another user's history. Phase 8: user_id is now a verified Clerk user
# id string (e.g. `user_2abc123def`) rather than a UUID — the key shape is
# unchanged. Pre-phase-8 anonymous-UUID keys are abandoned; the 30-day TTL
# retires them naturally.
SESSION_KEY_PREFIX = "chat:user:"


class SessionStore:
    """Redis-backed sliding window of `ChatMessage`s, keyed by (user_id, session_id).

    Each (user, session) pair is one Redis LIST at
    `chat:user:{user_id}:session:{session_id}:messages`. Reads use LRANGE;
    appends use a single MULTI/EXEC pipeline so the trim and TTL refresh always
    travel with the push.
    """

    def __init__(self, redis: Redis, *, max_messages: int, ttl_seconds: int) -> None:
        self.redis = redis
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds

    def _key(self, user_id: str, session_id: UUID) -> str:
        return f"{SESSION_KEY_PREFIX}{user_id}:session:{session_id}:messages"

    async def get_history(self, user_id: str, session_id: UUID) -> list[ChatMessage]:
        items = await self.redis.lrange(self._key(user_id, session_id), 0, -1)
        return [ChatMessage.model_validate_json(item) for item in items]

    async def append_turn(
        self,
        user_id: str,
        session_id: UUID,
        *,
        user_msg: ChatMessage,
        assistant_msg: ChatMessage,
    ) -> None:
        key = self._key(user_id, session_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, user_msg.model_dump_json(), assistant_msg.model_dump_json())
            pipe.ltrim(key, -self.max_messages, -1)
            pipe.expire(key, self.ttl_seconds)
            await pipe.execute()

    async def clear(self, user_id: str, session_id: UUID) -> None:
        await self.redis.delete(self._key(user_id, session_id))


def get_session_store() -> SessionStore:
    return SessionStore(
        redis_client,
        max_messages=settings.session_history_turns * 2,
        ttl_seconds=settings.session_ttl_seconds,
    )
