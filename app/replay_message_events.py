"""Replay stored edit/revoke webhooks after message-target parsing fixes.

Run inside the API container with: python -m app.replay_message_events
The operation is idempotent: it reapplies the final text/revoked timestamp
recorded in webhook_log and never calls WhatsApp.
"""

import asyncio
from typing import Any

from psycopg import AsyncConnection

from app.config import settings
from app.sync import handle_event


async def replay(conn: AsyncConnection[Any]) -> dict[str, int]:
    cur = await conn.execute(
        """
        SELECT event, payload, received_at
        FROM webhook_log
        WHERE event IN ('message.edited', 'message.revoked')
        ORDER BY received_at, id
        """
    )
    stats = {"message.edited": 0, "message.revoked": 0}
    for event, payload, received_at in await cur.fetchall():
        await handle_event(conn, event, payload, 0, event_time=received_at)
        stats[event] += 1
    await conn.commit()
    return stats


async def main() -> None:
    conn: AsyncConnection[Any] = await AsyncConnection.connect(
        settings.database_url
    )
    try:
        stats = await replay(conn)
    finally:
        await conn.close()
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
