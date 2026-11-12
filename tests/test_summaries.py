from datetime import datetime, timedelta, timezone

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from app import main
from app.config import settings
from app.main import app
from app.sync import upsert_message
from app.waha import WahaError

HEADERS: dict[str, str] = {}

BASE_TS = 1767225600  # matches tests/test_read.py's epoch anchor


def _msg(n: int, body: str, chat: str = "5511999@c.us") -> dict:
    return {
        "id": f"s{n}",
        "timestamp": BASE_TS + n * 60,
        "from": chat,
        "to": "5511000@c.us",
        "fromMe": False,
        "body": body,
        "hasMedia": False,
        "ack": 1,
    }


def _ts(n: int) -> datetime:
    return datetime.fromtimestamp(BASE_TS + n * 60, tz=timezone.utc)


@pytest.fixture
async def seeded(pool: AsyncConnectionPool) -> AsyncConnectionPool:
    # pool.connection() commits on clean exit, so rows are visible to
    # handlers below, which acquire their own connection from the pool.
    async with pool.connection() as conn:
        await upsert_message(conn, _msg(1, "oi"), 30)
        await upsert_message(conn, _msg(2, "tudo bem?"), 30)
        await upsert_message(conn, _msg(3, "outro chat", chat="5511777@c.us"), 30)
    return pool


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    )


async def test_put_creates_then_updates_summary(
    seeded: AsyncConnectionPool,
) -> None:
    async with await _client() as client:
        body = {
            "summary": "resumo inicial",
            "covered_through_ts": _ts(1).isoformat(),
            "covered_message_count": 1,
            "model": "agno/test",
        }
        r = await client.put(
            "/api/summaries/5511999@c.us", json=body, headers=HEADERS
        )
        assert r.status_code == 200
        data = r.json()
        assert data["summary"] == "resumo inicial"
        assert data["covered_message_count"] == 1

        body2 = {
            "summary": "resumo atualizado",
            "covered_through_ts": _ts(2).isoformat(),
            "covered_message_count": 2,
            "model": "agno/test",
        }
        r2 = await client.put(
            "/api/summaries/5511999@c.us", json=body2, headers=HEADERS
        )
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["summary"] == "resumo atualizado"
        assert data2["covered_message_count"] == 2


async def test_put_unknown_chat_returns_404(pool: AsyncConnectionPool) -> None:
    async with await _client() as client:
        body = {
            "summary": "x",
            "covered_through_ts": _ts(1).isoformat(),
            "covered_message_count": 1,
        }
        r = await client.put(
            "/api/summaries/nao@c.us", json=body, headers=HEADERS
        )
        assert r.status_code == 404


async def test_put_out_of_order_does_not_move_watermark_backwards(
    seeded: AsyncConnectionPool,
) -> None:
    async with await _client() as client:
        body_new = {
            "summary": "resumo mais recente",
            "covered_through_ts": _ts(2).isoformat(),
            "covered_message_count": 2,
            "model": "agno/a",
        }
        r = await client.put(
            "/api/summaries/5511999@c.us", json=body_new, headers=HEADERS
        )
        assert r.status_code == 200

        # A late/out-of-order worker result with an OLDER watermark arrives.
        body_old = {
            "summary": "resumo velho, atrasado",
            "covered_through_ts": _ts(1).isoformat(),
            "covered_message_count": 1,
            "model": "agno/b",
        }
        r2 = await client.put(
            "/api/summaries/5511999@c.us", json=body_old, headers=HEADERS
        )
        assert r2.status_code == 200
        data = r2.json()
        assert datetime.fromisoformat(data["covered_through_ts"]) == _ts(2)
        assert data["summary"] == "resumo mais recente"
        assert data["covered_message_count"] == 2


async def test_pending_count_is_derived(seeded: AsyncConnectionPool) -> None:
    async with seeded.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_summaries
              (chat_id, summary, covered_through_ts, covered_message_count)
            VALUES (%s, %s, %s, %s)
            """,
            ("5511999@c.us", "resumo", _ts(1), 1),
        )
    # message 2 (ts(2)) is newer than the watermark ts(1) -> 1 pending.
    async with await _client() as client:
        r = await client.get("/api/summaries", headers=HEADERS)
        assert r.status_code == 200
        item = next(i for i in r.json() if i["chat_id"] == "5511999@c.us")
        assert item["pending_count"] == 1


async def test_only_stale_filters(seeded: AsyncConnectionPool) -> None:
    async with seeded.connection() as conn:
        # up to date: covered through the newest message.
        await conn.execute(
            """
            INSERT INTO chat_summaries
              (chat_id, summary, covered_through_ts, covered_message_count)
            VALUES (%s, %s, %s, %s)
            """,
            ("5511999@c.us", "em dia", _ts(2), 2),
        )
        # stale: covered through an older message than the newest one.
        await conn.execute(
            """
            INSERT INTO chat_summaries
              (chat_id, summary, covered_through_ts, covered_message_count)
            VALUES (%s, %s, %s, %s)
            """,
            ("5511777@c.us", "atrasado", _ts(1), 1),
        )

    async with await _client() as client:
        r = await client.get(
            "/api/summaries", params={"only_stale": "true"}, headers=HEADERS
        )
        assert r.status_code == 200
        ids = [i["chat_id"] for i in r.json()]
        assert ids == ["5511777@c.us"]


async def test_pending_includes_never_summarized_excludes_empty_chats(
    seeded: AsyncConnectionPool,
) -> None:
    async with seeded.connection() as conn:
        # A chat with zero messages: backfill hasn't reached it. Must be
        # excluded even though it has no summary row either.
        await conn.execute(
            "INSERT INTO chats (id, name) VALUES (%s, %s)",
            ("5511000@c.us", "vazio"),
        )

    async with await _client() as client:
        r = await client.get("/api/summaries/pending", headers=HEADERS)
        assert r.status_code == 200
        ids = [i["chat_id"] for i in r.json()]
        assert "5511999@c.us" in ids  # never summarized, has messages
        assert "5511777@c.us" in ids
        assert "5511000@c.us" not in ids  # zero messages, excluded
        never = next(i for i in r.json() if i["chat_id"] == "5511999@c.us")
        assert never["covered_through_ts"] is None


async def test_overview_reports_unreachable_when_waha_down(
    seeded: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_status() -> dict:
        raise WahaError("connection refused")

    monkeypatch.setattr(main.waha, "status", fail_status)

    async with await _client() as client:
        r = await client.get("/api/overview", headers=HEADERS)
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["status"] == "UNREACHABLE"
        assert "backfill" in data
        assert "summaries" in data
        assert data["backfill"]["messages"] >= 2
