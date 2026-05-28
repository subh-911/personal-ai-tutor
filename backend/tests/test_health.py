from httpx import AsyncClient


async def test_health_reports_postgres_and_redis_up(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200, (
        f"/health returned {response.status_code}: {response.text}. "
        "Is `docker compose up -d` running?"
    )
    body = response.json()
    assert body["postgres"] is True, f"Postgres unreachable: {body}"
    assert body["redis"] is True, f"Redis unreachable: {body}"
    assert body["status"] == "ok"
