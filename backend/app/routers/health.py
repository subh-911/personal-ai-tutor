import asyncio

from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.redis_client import redis as redis_client
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


async def _check_postgres() -> bool:
    try:
        async with async_session_maker() as session:  # type: AsyncSession
            result = await session.execute(text("SELECT 1"))
            return result.scalar() == 1
    except Exception:
        return False


async def _check_redis(client: Redis) -> bool:
    try:
        return bool(await client.ping())
    except Exception:
        return False


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness + dependency check",
    responses={
        200: {"description": "All dependencies healthy."},
        503: {"description": "One or more dependencies are unreachable.", "model": HealthResponse},
    },
)
async def health(response: Response) -> HealthResponse:
    postgres_ok, redis_ok = await asyncio.gather(
        _check_postgres(),
        _check_redis(redis_client),
    )
    all_ok = postgres_ok and redis_ok
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        postgres=postgres_ok,
        redis=redis_ok,
    )
