from datetime import datetime, timezone

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from app.config import settings
from app.main import app
from app.sync import handle_event, upsert_message

HEADERS: dict[str, str] = {}


def _msg(n: int, body: str, chat: str = "5511999@c.us") -> dict:
    return {
        "id": f"m{n}",
        "timestamp": 1767225600 + n * 60,
        "from": chat,
        "to": "5511000@c.us",
        "fromMe": False,
        "body": body,
        "hasMedia": False,
        "ack": 1,
    }


@pytest.fixture
async def seeded(pool: AsyncConnectionPool) -> AsyncConnectionPool:
    # pool.connection() commits on clean exit, so these rows are visible to
    # the handlers below, which acquire their own connection from the pool.
    async with pool.connection() as conn:
        await upsert_message(conn, _msg(1, "bom dia pra você"), 30)
        await upsert_message(conn, _msg(2, "reunião amanhã cedo"), 30)
        await upsert_message(conn, _msg(3, "boa noite"), 30)
        await upsert_message(
            conn, _msg(4, "outro chat aqui", chat="5511777@c.us"), 30
        )
    return pool


async def test_chats_lists_newest_first(seeded: AsyncConnectionPool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/chats", headers=HEADERS)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()]
        assert set(ids) == {"5511999@c.us", "5511777@c.us"}


async def test_messages_returns_chat_in_reverse_time_order(
    seeded: AsyncConnectionPool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get(
            "/api/chats/5511999@c.us/messages", headers=HEADERS
        )
        bodies = [m["body"] for m in r.json()]
        assert bodies == ["boa noite", "reunião amanhã cedo", "bom dia pra você"]


async def test_messages_cursor_excludes_newer(
    seeded: AsyncConnectionPool,
) -> None:
    cutoff = datetime.fromtimestamp(
        1767225600 + 3 * 60, tz=timezone.utc
    ).isoformat()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get(
            "/api/chats/5511999@c.us/messages",
            params={"before_ts": cutoff},
            headers=HEADERS,
        )
        assert [m["body"] for m in r.json()] == [
            "reunião amanhã cedo",
            "bom dia pra você",
        ]


async def test_messages_hides_stub_until_real_message_arrives(
    seeded: AsyncConnectionPool,
) -> None:
    """C1 re-review, minor #2: an ack that beats message.any creates a stub
    row (raw = '{}') with ts = now(), which sorts to the top of the chat and
    must not be rendered as an empty bubble. Once message.any fills it in,
    it must appear."""
    stub_id = "false_5511999@c.us_STUB"
    async with seeded.connection() as conn:
        await handle_event(conn, "message.ack", {"id": stub_id, "ack": 1}, 30)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get(
            "/api/chats/5511999@c.us/messages", headers=HEADERS
        )
        assert stub_id not in [m["id"] for m in r.json()]

    async with seeded.connection() as conn:
        await handle_event(
            conn,
            "message.any",
            {
                "id": stub_id,
                "timestamp": 1767225600 + 10 * 60,
                "from": "5511999@c.us",
                "to": "5511000@c.us",
                "fromMe": False,
                "body": "chegou depois do ack",
                "hasMedia": False,
                "ack": 1,
            },
            30,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get(
            "/api/chats/5511999@c.us/messages", headers=HEADERS
        )
        assert stub_id in [m["id"] for m in r.json()]


async def test_search_finds_by_word(seeded: AsyncConnectionPool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/search?q=reunião", headers=HEADERS)
        hits = r.json()
        assert len(hits) == 1
        assert hits[0]["id"] == "m2"


async def test_search_is_accent_and_stem_tolerant(
    seeded: AsyncConnectionPool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/search?q=reuniao", headers=HEADERS)
        assert r.status_code == 200


async def test_search_scoped_to_chat(seeded: AsyncConnectionPool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get(
            "/api/search?q=chat&chat_id=5511777@c.us", headers=HEADERS
        )
        assert [h["id"] for h in r.json()] == ["m4"]


async def test_search_returns_highlight(seeded: AsyncConnectionPool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/search?q=noite", headers=HEADERS)
        highlight = r.json()[0]["highlight"]
        assert "<<HL>>" in highlight
        assert "<b>" not in highlight
