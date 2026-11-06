"""Repair of the chat_id mis-routing, against a seeded database that looks
exactly like production did: the owner's group messages filed under their own
LID, and a chat the backfill hasn't reached yet that must survive."""

from datetime import datetime, timezone
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from app.repair import repair

GROUP = "120363000000000001@g.us"
OWNER_LID = "100000000000001@lid"
CONTACT = "100000000000002@lid"
EMPTY_CHAT = "5511777@c.us"  # backfill hasn't reached it: legitimately empty

TS = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _payload(message_id: str, **over: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "timestamp": int(TS.timestamp()),
        "fromMe": message_id.startswith("true_"),
        "body": "oi",
        "hasMedia": False,
        **over,
    }


async def _seed(conn: AsyncConnection[Any]) -> None:
    # CONTACT is deliberately absent: the outgoing DM's chat may not exist
    # yet, and messages.chat_id is an FK -- the repair must create it.
    for chat_id in (GROUP, OWNER_LID, EMPTY_CHAT):
        await conn.execute(
            "INSERT INTO chats (id, is_group) VALUES (%s, %s)",
            (chat_id, chat_id.endswith("@g.us")),
        )
    rows: list[tuple[str, str, bool, dict[str, Any]]] = [
        # the owner's own group messages, dumped on their LID
        (
            f"true_{GROUP}_AAA_{OWNER_LID}",
            OWNER_LID,
            True,
            _payload(f"true_{GROUP}_AAA_{OWNER_LID}", **{"from": GROUP, "to": OWNER_LID}),
        ),
        (
            f"true_{GROUP}_BBB_{OWNER_LID}",
            OWNER_LID,
            True,
            _payload(f"true_{GROUP}_BBB_{OWNER_LID}", **{"from": GROUP, "to": OWNER_LID}),
        ),
        # an incoming group message, already on the right chat
        (
            f"false_{GROUP}_CCC_5511900000000@c.us",
            GROUP,
            False,
            _payload(f"false_{GROUP}_CCC_5511900000000@c.us", **{"from": GROUP}),
        ),
        # an outgoing DM that the old code filed on the wrong chat
        (
            f"true_{CONTACT}_DDD",
            OWNER_LID,
            True,
            _payload(f"true_{CONTACT}_DDD", **{"from": CONTACT, "to": None}),
        ),
        # a stub row (side-channel event, no payload): must be left alone
        (f"false_{GROUP}_EEE_x@c.us", GROUP, False, {}),
    ]
    for message_id, chat_id, from_me, raw in rows:
        await conn.execute(
            """
            INSERT INTO messages (id, chat_id, ts, from_me, raw)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (message_id, chat_id, TS, from_me, Json(raw)),
        )
    for chat_id in (GROUP, OWNER_LID, EMPTY_CHAT):
        await conn.execute(
            "INSERT INTO sync_state (chat_id, cursor_ts, done) VALUES (%s, %s, true)",
            (chat_id, TS),
        )
    await conn.commit()


async def _chat_of(conn: AsyncConnection[Any], message_id: str) -> str | None:
    cur = await conn.execute(
        "SELECT chat_id FROM messages WHERE id = %s", (message_id,)
    )
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def test_repair_moves_rows_and_drops_the_fabricated_chat(
    pool: AsyncConnectionPool,
) -> None:
    async with await AsyncConnection.connect(pool.conninfo) as conn:
        await _seed(conn)
        stats = await repair(conn)

        assert stats["rows_moved"] == 3
        assert stats["chats_created"] == 1
        assert stats["chats_deleted"] == 1

        assert await _chat_of(conn, f"true_{GROUP}_AAA_{OWNER_LID}") == GROUP
        assert await _chat_of(conn, f"true_{GROUP}_BBB_{OWNER_LID}") == GROUP
        assert await _chat_of(conn, f"true_{CONTACT}_DDD") == CONTACT
        assert await _chat_of(conn, f"false_{GROUP}_EEE_x@c.us") == GROUP

        cur = await conn.execute("SELECT id FROM chats ORDER BY id")
        assert [r[0] for r in await cur.fetchall()] == sorted(
            [GROUP, CONTACT, EMPTY_CHAT]
        )

        # the chat the backfill hasn't reached keeps its sync_state
        cur = await conn.execute(
            "SELECT chat_id, cursor_ts, done FROM sync_state ORDER BY chat_id"
        )
        state = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        assert state[EMPTY_CHAT] == (TS, True)
        assert state[GROUP] == (None, False)  # re-walk what the mis-routing hid
        assert OWNER_LID not in state

        cur = await conn.execute(
            "SELECT last_msg_ts FROM chats WHERE id = %s", (CONTACT,)
        )
        assert (await cur.fetchone())[0] == TS


async def test_repair_is_idempotent(pool: AsyncConnectionPool) -> None:
    async with await AsyncConnection.connect(pool.conninfo) as conn:
        await _seed(conn)
        await repair(conn)
        cur = await conn.execute(
            "SELECT id, chat_id FROM messages ORDER BY id"
        )
        before = await cur.fetchall()

        stats = await repair(conn)
        assert stats == {
            "rows_moved": 0,
            "chats_created": 0,
            "chats_deleted": 0,
            "sync_state_reset": 0,
        }
        cur = await conn.execute(
            "SELECT id, chat_id FROM messages ORDER BY id"
        )
        assert await cur.fetchall() == before
        cur = await conn.execute("SELECT count(*) FROM chats")
        assert (await cur.fetchone())[0] == 3
