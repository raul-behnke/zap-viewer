from datetime import datetime, timezone

from psycopg.types.json import Json

from app.replay_message_events import replay


async def test_replay_repairs_gows_edit_and_revoke_targets(pool) -> None:
    edited_at = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    revoked_at = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO chats (id, name, is_group) "
            "VALUES ('5511999@c.us', 'Marcos', false)"
        )
        await conn.execute(
            """
            INSERT INTO messages (
              id, chat_id, ts, from_me, body, type, has_media, raw
            ) VALUES
              ('true_5511999@c.us_EDITRAW', '5511999@c.us', now(), true,
               'antes', 'chat', false, %s),
              ('true_5511999@c.us_DELETERAW', '5511999@c.us', now(), true,
               'apagar', 'chat', false, %s)
            """,
            (
                Json({"_data": {"Info": {"ID": "EDITRAW"}}}),
                Json({"_data": {"Info": {"ID": "DELETERAW"}}}),
            ),
        )
        await conn.execute(
            """
            INSERT INTO webhook_log (event, payload, received_at) VALUES
              ('message.edited', %s, %s),
              ('message.revoked', %s, %s)
            """,
            (
                Json({"editedMessageId": "EDITRAW", "body": "depois"}),
                edited_at,
                Json({"revokedMessageId": "DELETERAW"}),
                revoked_at,
            ),
        )
        stats = await replay(conn)
        assert stats == {"message.edited": 1, "message.revoked": 1}

        cur = await conn.execute(
            "SELECT body, edited_at FROM messages "
            "WHERE id = 'true_5511999@c.us_EDITRAW'"
        )
        assert await cur.fetchone() == ("depois", edited_at)
        cur = await conn.execute(
            "SELECT revoked_at FROM messages "
            "WHERE id = 'true_5511999@c.us_DELETERAW'"
        )
        assert await cur.fetchone() == (revoked_at,)

        # Replaying again keeps the same final state and timestamps.
        await replay(conn)
        cur = await conn.execute(
            "SELECT body, edited_at FROM messages "
            "WHERE id = 'true_5511999@c.us_EDITRAW'"
        )
        assert await cur.fetchone() == ("depois", edited_at)
