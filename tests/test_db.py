async def test_schema_creates_expected_tables(pool):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        names = {row[0] for row in await cur.fetchall()}
    assert {
        "chats",
        "messages",
        "sync_state",
        "webhook_log",
        "sent_log",
        "message_action_log",
    } <= names


async def test_schema_is_idempotent(pool):
    from app.db import SCHEMA_PATH

    async with pool.connection() as conn:
        await conn.execute(SCHEMA_PATH.read_text())
