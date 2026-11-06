"""One-shot repair of chat_id mis-routing. Run: python -m app.repair

Before the routing fix, chat_id came from `to`/`from`: every message the
owner sent in a group was filed under a fabricated chat keyed by their own
LID, and every outgoing DM was skipped. This re-derives chat_id from `raw`
with the fixed chat_id_of() and moves the rows that landed wrong.

Idempotent: a second run finds no mismatches and therefore moves nothing,
deletes nothing and resets nothing.
"""

import asyncio
from collections import defaultdict
from typing import Any

from psycopg import AsyncConnection

from app.sync import _STUB_RAW, chat_id_of


async def repair(conn: AsyncConnection) -> dict[str, int]:
    cur = await conn.execute(
        "SELECT id, chat_id, raw FROM messages WHERE raw <> %s::jsonb",
        (_STUB_RAW,),
    )
    # Stub rows (raw = '{}') carry no payload to re-derive from; their
    # chat_id already came from the message id, which is what we now trust.
    moves: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )  # source chat -> destination chat -> message ids
    destinations: set[str] = set()

    for message_id, chat_id, raw in await cur.fetchall():
        correct = chat_id_of(raw)
        if not correct:
            continue
        destinations.add(correct)
        if correct != chat_id:
            moves[chat_id][correct].append(message_id)

    stats: dict[str, int] = {
        "rows_moved": 0,
        "chats_created": 0,
        "chats_deleted": 0,
        "sync_state_reset": 0,
    }
    touched: set[str] = set()

    # One transaction per source chat: a failure can't leave a conversation
    # half-moved.
    for source, by_dest in moves.items():
        async with conn.transaction():
            for dest, message_ids in by_dest.items():
                created = await conn.execute(
                    """
                    INSERT INTO chats (id, is_group) VALUES (%s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (dest, dest.endswith("@g.us")),
                )
                stats["chats_created"] += created.rowcount
                moved = await conn.execute(
                    "UPDATE messages SET chat_id = %s WHERE id = ANY(%s)",
                    (dest, message_ids),
                )
                stats["rows_moved"] += moved.rowcount
                touched.add(dest)
            touched.add(source)

    if not touched:
        return stats

    async with conn.transaction():
        affected = sorted(touched)
        await conn.execute(
            """
            UPDATE chats SET
              last_msg_ts = (SELECT max(ts) FROM messages WHERE chat_id = chats.id),
              updated_at = now()
            WHERE id = ANY(%s)
            """,
            (affected,),
        )

        # A fabricated chat is one we just emptied (it was a move source) that
        # no message legitimately belongs to (never a correct destination). A
        # chat the backfill simply hasn't reached is empty too, but was never a
        # move source, so it isn't in this list at all.
        suspects = sorted(set(moves) - destinations)
        if suspects:
            for table in ("sync_state", "chat_summaries"):
                await conn.execute(
                    f"""
                    DELETE FROM {table} WHERE chat_id = ANY(%s)
                      AND NOT EXISTS (
                        SELECT 1 FROM messages WHERE chat_id = {table}.chat_id
                      )
                    """,  # noqa: S608 - table name is a literal from the tuple
                    (suspects,),
                )
            deleted = await conn.execute(
                """
                DELETE FROM chats WHERE id = ANY(%s)
                  AND NOT EXISTS (SELECT 1 FROM messages WHERE chat_id = chats.id)
                """,
                (suspects,),
            )
            stats["chats_deleted"] += deleted.rowcount

        # Re-walk the chats whose contents moved, to pick up what the
        # mis-routing hid. Only these: every other chat's deep-walk cursor
        # stays untouched.
        reset = await conn.execute(
            """
            UPDATE sync_state SET cursor_ts = NULL, done = false, updated_at = now()
            WHERE chat_id = ANY(%s)
            """,
            (affected,),
        )
        stats["sync_state_reset"] += reset.rowcount

    return stats


async def main() -> None:
    from app.config import settings

    # autocommit=True so each `conn.transaction()` below is a real transaction.
    # Without it, the SELECT that computes the moves opens an implicit
    # transaction, every `transaction()` block becomes a nested savepoint that
    # releases without committing, and conn.close() discards the whole repair —
    # which reports rows_moved > 0 and changes nothing.
    conn: AsyncConnection[Any] = await AsyncConnection.connect(
        settings.database_url, autocommit=True
    )
    try:
        stats = await repair(conn)
    finally:
        await conn.close()
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
