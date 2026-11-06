import hashlib
import hmac as hmac_mod
from datetime import datetime, timezone
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

import app.sync as sync_module
from app.sync import (
    backfill_all,
    backfill_chat,
    backfill_in_progress,
    chat_id_of,
    handle_event,
    parse_message,
    start_backfill_pass,
    upsert_chat,
    upsert_message,
    verify_hmac,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

INCOMING = {
    "id": "false_5511999@c.us_ABC",
    "timestamp": 1767225600,
    "from": "5511999@c.us",
    "to": "5511000@c.us",
    "fromMe": False,
    "body": "bom dia",
    "hasMedia": False,
    "ack": 1,
    "media": None,
    "replyTo": None,
    "_data": {},
}

OUTGOING = {**INCOMING, "id": "true_5511999@c.us_XYZ", "fromMe": True}

WITH_MEDIA = {
    **INCOMING,
    "id": "false_5511999@c.us_MED",
    "body": "olha isso",
    "hasMedia": True,
    "media": {
        "url": "http://waha:3000/api/files/x.jpg",
        "mimetype": "image/jpeg",
        "filename": "x.jpg",
        "error": None,
    },
}

GROUP = {
    **INCOMING,
    "id": "false_1203@g.us_GRP",
    "from": "1203@g.us",
    "participant": "5511888@c.us",
}


def test_chat_id_uses_from_for_incoming() -> None:
    assert chat_id_of(INCOMING) == "5511999@c.us"


def test_chat_id_uses_the_id_for_outgoing() -> None:
    # `to` is the wrong answer here: for a DM the chat is the contact, and
    # WAHA sends `to` as null anyway (see REAL_DM_OUT below).
    assert chat_id_of(OUTGOING) == "5511999@c.us"


# ---------------------------------------------------------------------------
# Routing against payloads copied verbatim from the live mirror: the chat is
# the second segment of the message id, in both directions and both chat
# types. `from`/`to` are not the chat (group in `from` + participant in `to`
# for groups; null `to` for DMs).
# ---------------------------------------------------------------------------

REAL_GROUP_OUT = {
    "id": "true_120363000000000001@g.us_2AE19E6C59E3D4FFF249_100000000000001@lid",
    "timestamp": 1767225600,
    "from": "120363000000000001@g.us",
    "to": "100000000000001@lid",
    "fromMe": True,
    "participant": "100000000000001@lid",
    "body": "eu no grupo",
    "hasMedia": False,
}

REAL_GROUP_IN = {
    **REAL_GROUP_OUT,
    "id": "false_120363000000000001@g.us_3BB1B764ADED5E1B5FFB_5511900000000@c.us",
    "fromMe": False,
    "participant": "5511900000000@c.us",
    "body": "outra pessoa no grupo",
}

REAL_DM_OUT = {
    "id": "true_100000000000002@lid_3BF9A1D5DA096E4F1DA9",
    "timestamp": 1767225600,
    "from": "100000000000002@lid",
    "to": None,
    "fromMe": True,
    "participant": None,
    "body": "eu na dm",
    "hasMedia": False,
}

REAL_DM_IN = {
    **REAL_DM_OUT,
    "id": "false_100000000000003@lid_3EB0CA608C165086B33A97",
    "from": "100000000000003@lid",
    "fromMe": False,
    "body": "ela na dm",
}


def test_outgoing_group_message_lands_on_the_group() -> None:
    row = parse_message(REAL_GROUP_OUT, 30, now=NOW)
    assert row["chat_id"] == "120363000000000001@g.us"
    assert row["sender_jid"] == "100000000000001@lid"


def test_incoming_group_message_lands_on_the_group_with_participant() -> None:
    row = parse_message(REAL_GROUP_IN, 30, now=NOW)
    assert row["chat_id"] == "120363000000000001@g.us"
    assert row["sender_jid"] == "5511900000000@c.us"


def test_outgoing_dm_lands_on_the_contact() -> None:
    row = parse_message(REAL_DM_OUT, 30, now=NOW)
    assert row["chat_id"] == "100000000000002@lid"
    # `from` is the contact, not the owner: don't attribute it to them.
    assert row["sender_jid"] is None


def test_incoming_dm_lands_on_the_contact() -> None:
    row = parse_message(REAL_DM_IN, 30, now=NOW)
    assert row["chat_id"] == "100000000000003@lid"
    assert row["sender_jid"] == "100000000000003@lid"


async def test_outgoing_dm_is_mirrored_not_skipped(
    pool: AsyncConnectionPool,
) -> None:
    """The old code derived None here and skipped every outgoing DM."""
    async with pool.connection() as conn:
        assert await upsert_message(conn, REAL_DM_OUT, 30) is True
        cur = await conn.execute("SELECT chat_id FROM messages WHERE id = %s",
                                 (REAL_DM_OUT["id"],))
        assert (await cur.fetchone())[0] == "100000000000002@lid"


async def test_outgoing_group_message_is_not_filed_under_the_owner(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        assert await upsert_message(conn, REAL_GROUP_OUT, 30) is True
        cur = await conn.execute("SELECT count(*) FROM chats WHERE id = %s",
                                 ("100000000000001@lid",))
        assert (await cur.fetchone())[0] == 0


def test_parse_basic_fields() -> None:
    row = parse_message(INCOMING, 30, now=NOW)
    assert row["id"] == "false_5511999@c.us_ABC"
    assert row["chat_id"] == "5511999@c.us"
    assert row["from_me"] is False
    assert row["body"] == "bom dia"
    assert row["ts"] == datetime.fromtimestamp(1767225600, tz=timezone.utc)
    assert row["has_media"] is False
    assert row["media_expires_at"] is None


def test_parse_media_sets_expiry_from_now() -> None:
    row = parse_message(WITH_MEDIA, 30, now=NOW)
    assert row["has_media"] is True
    assert row["media_url"] == "http://waha:3000/api/files/x.jpg"
    assert row["media_mime"] == "image/jpeg"
    assert row["media_expires_at"] == datetime(
        2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_parse_group_sender_is_participant() -> None:
    row = parse_message(GROUP, 30, now=NOW)
    assert row["chat_id"] == "1203@g.us"
    assert row["sender_jid"] == "5511888@c.us"


def test_parse_direct_sender_is_from() -> None:
    row = parse_message(INCOMING, 30, now=NOW)
    assert row["sender_jid"] == "5511999@c.us"


@pytest.fixture(autouse=True)
def _reset_backfill_pass_state() -> Any:
    # _current_pass is process-wide module state (the single-flight guard);
    # isolate tests that touch it from each other.
    sync_module._current_pass = None
    yield
    sync_module._current_pass = None


async def test_upsert_is_idempotent(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await upsert_message(conn, INCOMING, 30)
        await upsert_message(conn, INCOMING, 30)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 1


async def test_upsert_creates_chat_row(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await upsert_message(conn, INCOMING, 30)
        cur = await conn.execute(
            "SELECT id, is_group FROM chats WHERE id = %s", ("5511999@c.us",)
        )
        assert await cur.fetchone() == ("5511999@c.us", False)


async def test_upsert_marks_group_chats(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await upsert_message(conn, GROUP, 30)
        cur = await conn.execute(
            "SELECT is_group FROM chats WHERE id = %s", ("1203@g.us",)
        )
        assert (await cur.fetchone())[0] is True


async def test_second_upsert_does_not_overwrite_body(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await upsert_message(conn, INCOMING, 30)
        await upsert_message(conn, {**INCOMING, "body": "editado à força"}, 30)
        cur = await conn.execute(
            "SELECT body FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        assert (await cur.fetchone())[0] == "bom dia"


async def test_upsert_message_returns_true_on_insert_false_on_conflict(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        first = await upsert_message(conn, INCOMING, 30)
        second = await upsert_message(conn, INCOMING, 30)
        assert first is True
        assert second is False


def _sign(body: bytes, key: str) -> str:
    return hmac_mod.new(key.encode(), body, hashlib.sha512).hexdigest()


def test_verify_hmac_accepts_correct_signature() -> None:
    body = b'{"a":1}'
    assert verify_hmac(body, _sign(body, "segredo"), "segredo") is True


def test_verify_hmac_rejects_wrong_signature() -> None:
    assert verify_hmac(b'{"a":1}', "deadbeef", "segredo") is False


def test_verify_hmac_rejects_missing_header() -> None:
    assert verify_hmac(b'{"a":1}', None, "segredo") is False


def test_verify_hmac_rejects_body_tampering() -> None:
    sig = _sign(b'{"a":1}', "segredo")
    assert verify_hmac(b'{"a":2}', sig, "segredo") is False


async def test_handle_message_any_inserts(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 1


async def test_handle_ack_updates_without_touching_body(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await handle_event(
            conn, "message.ack", {"id": INCOMING["id"], "ack": 3}, 30
        )
        cur = await conn.execute(
            "SELECT ack, body FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        assert await cur.fetchone() == (3, "bom dia")


async def test_handle_ack_for_message_id_without_chat_encoding_is_a_noop(
    pool: AsyncConnectionPool,
) -> None:
    """WAHA ids embed "{fromMe}_{chatId}_{serial}"; an id that doesn't parse
    can't seed a stub (chat_id is NOT NULL, FK to chats), so this is the one
    case genuinely dropped, not a general "unknown message" free pass."""
    async with pool.connection() as conn:
        await handle_event(conn, "message.ack", {"id": "nope", "ack": 3}, 30)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 0


async def test_handle_ack_before_message_any_creates_stub_that_survives(
    pool: AsyncConnectionPool,
) -> None:
    """I3: an ack delivered before message.any (independent concurrent HTTP
    deliveries) must not be silently dropped, and the later message.any must
    not clobber the ack the stub already recorded."""
    async with pool.connection() as conn:
        await handle_event(
            conn, "message.ack", {"id": INCOMING["id"], "ack": 3}, 30
        )
        cur = await conn.execute(
            "SELECT ack, chat_id, body FROM messages WHERE id = %s",
            (INCOMING["id"],),
        )
        assert await cur.fetchone() == (3, "5511999@c.us", None)

        # message.any arrives later (INCOMING itself carries ack=1) and must
        # fill in the body without overwriting the ack already recorded.
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute(
            "SELECT ack, body FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        assert await cur.fetchone() == (3, "bom dia")


async def test_handle_revoked_before_message_any_stays_revoked(
    pool: AsyncConnectionPool,
) -> None:
    """I3: a revoke delivered before message.any must not vanish — a
    revoked message must never resurrect as visible again."""
    async with pool.connection() as conn:
        await handle_event(
            conn, "message.revoked", {"before": {"id": INCOMING["id"]}}, 30
        )
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute(
            "SELECT revoked_at, body FROM messages WHERE id = %s",
            (INCOMING["id"],),
        )
        revoked_at, body = await cur.fetchone()
        assert revoked_at is not None
        assert body == "bom dia"


async def test_handle_edited_before_message_any_survives(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await handle_event(
            conn, "message.edited", {"id": INCOMING["id"], "body": "corrigido"}, 30
        )
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute(
            "SELECT body, edited_at FROM messages WHERE id = %s",
            (INCOMING["id"],),
        )
        body, edited_at = await cur.fetchone()
        assert body == "corrigido"
        assert edited_at is not None


async def test_gows_edited_event_resolves_raw_target_id(
    pool: AsyncConnectionPool,
) -> None:
    original = {
        **INCOMING,
        "id": "false_5511999@c.us_ORIGINALRAW",
        "_data": {"Info": {"ID": "ORIGINALRAW"}},
    }
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", original, 30)
        event_time = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        await handle_event(
            conn,
            "message.edited",
            {
                "id": "false_5511999@c.us_EDITEVENT",
                "editedMessageId": "ORIGINALRAW",
                "body": "corrigido pelo GOWS",
            },
            30,
            event_time=event_time,
        )
        cur = await conn.execute(
            "SELECT body, edited_at FROM messages WHERE id = %s",
            (original["id"],),
        )
        body, edited_at = await cur.fetchone()
        assert body == "corrigido pelo GOWS"
        assert edited_at == event_time


async def test_gows_revoked_event_resolves_raw_target_id(
    pool: AsyncConnectionPool,
) -> None:
    original = {
        **OUTGOING,
        "id": "true_5511999@c.us_REVOKERAW",
        "_data": {"Info": {"ID": "REVOKERAW"}},
    }
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", original, 30)
        event_time = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
        await handle_event(
            conn,
            "message.revoked",
            {
                "revokedMessageId": "REVOKERAW",
                "after": {"id": "true_5511999@c.us_REVOKEEVENT"},
            },
            30,
            event_time=event_time,
        )
        cur = await conn.execute(
            "SELECT body, revoked_at FROM messages WHERE id = %s",
            (original["id"],),
        )
        body, revoked_at = await cur.fetchone()
        assert body == "bom dia"
        assert revoked_at == event_time


async def test_handle_reaction_before_message_any_survives(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await handle_event(
            conn,
            "message.reaction",
            {
                "from": "5511999@c.us",
                "reaction": {"text": "👍", "messageId": INCOMING["id"]},
            },
            30,
        )
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute(
            "SELECT reactions, body FROM messages WHERE id = %s",
            (INCOMING["id"],),
        )
        reactions, body = await cur.fetchone()
        assert reactions[0]["text"] == "👍"
        assert body == "bom dia"


async def test_handle_revoked_marks_message(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await handle_event(
            conn, "message.revoked", {"before": {"id": INCOMING["id"]}}, 30
        )
        cur = await conn.execute(
            "SELECT revoked_at FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        assert (await cur.fetchone())[0] is not None


async def test_handle_reaction_appends(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await handle_event(
            conn,
            "message.reaction",
            {
                "id": "reaction_1",
                "from": "5511999@c.us",
                "reaction": {"text": "👍", "messageId": INCOMING["id"]},
            },
            30,
        )
        cur = await conn.execute(
            "SELECT reactions FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        reactions = (await cur.fetchone())[0]
        assert reactions[0]["text"] == "👍"


async def test_handle_reaction_redelivery_does_not_duplicate(
    pool: AsyncConnectionPool,
) -> None:
    """I2: WAHA's only delivery guarantee is retry (spec §9), so the same
    reaction event redelivered must not produce two entries."""
    reaction_event = {
        "from": "5511999@c.us",
        "reaction": {"text": "👍", "messageId": INCOMING["id"]},
    }
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await handle_event(conn, "message.reaction", reaction_event, 30)
        await handle_event(conn, "message.reaction", reaction_event, 30)
        cur = await conn.execute(
            "SELECT reactions FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        reactions = (await cur.fetchone())[0]
        assert len(reactions) == 1
        assert reactions[0]["text"] == "👍"


async def test_handle_reaction_empty_text_removes_it(
    pool: AsyncConnectionPool,
) -> None:
    """I2: an empty text is WhatsApp's reaction-removal signal, not a
    reaction to append."""
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await handle_event(
            conn,
            "message.reaction",
            {
                "from": "5511999@c.us",
                "reaction": {"text": "👍", "messageId": INCOMING["id"]},
            },
            30,
        )
        await handle_event(
            conn,
            "message.reaction",
            {
                "from": "5511999@c.us",
                "reaction": {"text": "", "messageId": INCOMING["id"]},
            },
            30,
        )
        cur = await conn.execute(
            "SELECT reactions FROM messages WHERE id = %s", (INCOMING["id"],)
        )
        assert (await cur.fetchone())[0] == []


async def test_backfill_then_webhook_yields_one_row(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await upsert_message(conn, INCOMING, 30)
        await handle_event(conn, "message.any", INCOMING, 30)
        cur = await conn.execute("SELECT count(*), max(body) FROM messages")
        assert await cur.fetchone() == (1, "bom dia")


async def test_webhook_then_backfill_yields_one_row(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", INCOMING, 30)
        await upsert_message(conn, INCOMING, 30)
        cur = await conn.execute("SELECT count(*), max(body) FROM messages")
        assert await cur.fetchone() == (1, "bom dia")


class FakeWaha:
    """Serves messages newest-first, honoring filter.timestamp.lte."""

    def __init__(self, chats: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
        self._chats = chats
        self._messages = sorted(
            messages, key=lambda m: m["timestamp"], reverse=True
        )
        self.calls = 0

    async def chats_overview(self, limit: int, offset: int) -> list[dict[str, Any]]:
        return self._chats[offset : offset + limit]

    async def chat_messages(
        self, chat_id: str, limit: int, before_ts: int | None
    ) -> list[dict[str, Any]]:
        self.calls += 1
        rows = [
            m
            for m in self._messages
            if chat_id_of(m) == chat_id
            and (before_ts is None or m["timestamp"] <= before_ts)
        ]
        return rows[:limit]


def _msg(n: int) -> dict[str, Any]:
    return {
        "id": f"false_5511999@c.us_{n:03d}",
        "timestamp": 1767225600 - n * 60,
        "from": "5511999@c.us",
        "to": "5511000@c.us",
        "fromMe": False,
        "body": f"mensagem {n}",
        "hasMedia": False,
        "ack": 1,
    }


async def test_backfill_chat_pages_through_all_messages(
    pool: AsyncConnectionPool,
) -> None:
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(n) for n in range(10)],
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        written = await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=3)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 10
    assert written == 10


async def test_backfill_chat_marks_done(pool: AsyncConnectionPool) -> None:
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(n) for n in range(3)],
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=10)
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True


async def test_backfill_rerun_writes_nothing_new(pool: AsyncConnectionPool) -> None:
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(n) for n in range(5)],
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=2)
    async with pool.connection() as conn:
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=2)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 5


async def test_backfill_all_creates_chats_and_messages(
    pool: AsyncConnectionPool,
) -> None:
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(n) for n in range(4)],
    )
    result = await backfill_all(pool, waha, 30)
    assert result == {"chats": 1, "messages": 4}


async def test_backfill_all_survives_a_failing_chat(pool: AsyncConnectionPool) -> None:
    class Exploding(FakeWaha):
        async def chat_messages(
            self, chat_id: str, limit: int, before_ts: int | None
        ) -> list[dict[str, Any]]:
            if chat_id == "boom@c.us":
                raise RuntimeError("waha caiu")
            return await super().chat_messages(chat_id, limit, before_ts)

    waha = Exploding(
        chats=[{"id": "boom@c.us", "name": "Ruim"},
               {"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(n) for n in range(2)],
    )
    result = await backfill_all(pool, waha, 30)
    assert result["messages"] == 2


async def test_backfill_chat_resumes_after_crash(pool: AsyncConnectionPool) -> None:
    class CrashingWaha(FakeWaha):
        def __init__(self, *args: Any, crash_at: int, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.crash_at = crash_at

        async def chat_messages(
            self, chat_id: str, limit: int, before_ts: int | None
        ) -> list[dict[str, Any]]:
            if self.calls + 1 == self.crash_at:
                self.calls += 1
                raise RuntimeError("waha caiu")
            return await super().chat_messages(chat_id, limit, before_ts)

    all_messages = [_msg(n) for n in range(10)]
    crashing = CrashingWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=all_messages,
        crash_at=3,
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)

    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            await backfill_chat(conn, crashing, "5511999@c.us", 30, page_size=3)

    # Fresh connection: only what the crashed run actually committed is visible.
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT cursor_ts, done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] is not None, "cursor from completed pages must be durable"
        assert row[1] is False

    healthy = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=all_messages,
    )
    async with pool.connection() as conn:
        await backfill_chat(conn, healthy, "5511999@c.us", 30, page_size=3)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 10
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True


async def test_backfill_after_done_heals_a_webhook_gap(
    pool: AsyncConnectionPool,
) -> None:
    """I4: once a chat is done, a later pass must still be able to pick up
    messages newer than the cursor that the webhook missed while the API
    was down — the old code only ever paged older than the cursor."""
    initial = [_msg(n) for n in range(5)]
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}], messages=initial
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=10)
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True

    # Two messages newer than anything already synced, missed by the
    # webhook while the API was down.
    missed = [_msg(-1), _msg(-2)]
    healed_waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=missed + initial,
    )
    async with pool.connection() as conn:
        written = await backfill_chat(
            conn, healed_waha, "5511999@c.us", 30, page_size=10
        )
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 7
    assert written == 2


async def test_backfill_chat_boundary_tie_does_not_mark_done(
    pool: AsyncConnectionPool,
) -> None:
    tied_ts = 1767225600
    messages = [
        {
            "id": f"false_5511999@c.us_{n:03d}",
            "timestamp": tied_ts,
            "from": "5511999@c.us",
            "to": "5511000@c.us",
            "fromMe": False,
            "body": f"mensagem {n}",
            "hasMedia": False,
            "ack": 1,
        }
        for n in range(4)  # page_size + 1 messages sharing one timestamp
    ]
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=messages,
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=3)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 4
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is False


async def test_backfill_done_chat_recovers_deeper_history(
    pool: AsyncConnectionPool,
) -> None:
    """The production bug: GOWS keeps streaming older history into WAHA long
    after a chat's first walk finished. A chat marked done=true must still
    pick up messages older than its stored cursor on the next pass, not just
    converge at the top. Must FAIL against the pre-fix code (which only ever
    re-walks from now() for a done chat)."""
    # page_size (3) smaller than the message count so the top-convergence
    # pass alone can't accidentally sweep up the deeper history and mask
    # the bug -- it must stop as soon as it re-sees an already-known
    # message, same as production.
    initial = [_msg(n) for n in range(5)]
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}], messages=initial
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=4)
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True

    # WAHA has since received (from GOWS) 5 messages older than anything we
    # hold -- exactly what the deep walk must now go find.
    deeper = [_msg(n) for n in range(5, 10)]
    waha_with_history = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=initial + deeper,
    )
    async with pool.connection() as conn:
        written = await backfill_chat(
            conn, waha_with_history, "5511999@c.us", 30, page_size=4
        )
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 10
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True
    assert written == 5


async def test_backfill_done_chat_with_nothing_older_stays_done(
    pool: AsyncConnectionPool,
) -> None:
    """A done chat with genuinely no older history writes no new rows, stays
    done, and the deep-walk step costs exactly one request that returns
    empty (plus the top convergence request)."""
    initial = [_msg(n) for n in range(5)]
    waha = FakeWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}], messages=initial
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=10)

    calls_before = waha.calls
    async with pool.connection() as conn:
        written = await backfill_chat(conn, waha, "5511999@c.us", 30, page_size=10)
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 5
        cur = await conn.execute(
            "SELECT cursor_ts, done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        cursor_ts, done = await cur.fetchone()
        assert done is True
        assert cursor_ts == datetime.fromtimestamp(
            _msg(4)["timestamp"], tz=timezone.utc
        )
    assert written == 0
    # one request to converge at the top (hits an already-known message
    # immediately) + one request for the deep walk (comes back empty).
    assert waha.calls - calls_before == 2


# ---------------------------------------------------------------------------
# Bug 1: a message with a missing/null derived chat_id must not crash the
# page/chat walk or the webhook path.
# ---------------------------------------------------------------------------

BAD_TO_NULL = {
    "id": "true_null_BAD",
    "timestamp": 1767225600 - 30,
    "from": "5511999@c.us",
    "to": None,
    "fromMe": True,
    "body": "corrupted, to is null",
    "hasMedia": False,
    "ack": 1,
}


def test_chat_id_of_returns_none_for_null_field() -> None:
    assert chat_id_of(BAD_TO_NULL) is None


async def test_upsert_message_skips_null_chat_id_without_crashing(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        written = await upsert_message(conn, BAD_TO_NULL, 30)
        assert written is False
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 0


async def test_handle_message_any_with_null_chat_id_does_not_crash(
    pool: AsyncConnectionPool,
) -> None:
    async with pool.connection() as conn:
        await handle_event(conn, "message.any", BAD_TO_NULL, 30)  # must not raise
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 0


async def test_backfill_chat_skips_bad_message_and_completes_the_walk(
    pool: AsyncConnectionPool,
) -> None:
    """Realistic repro: a page of otherwise-valid messages with one message
    whose fromMe is true and `to` is null, mixed in. Must not abort the page
    or the chat -- all valid messages mirrored, walk completes and the chat
    is marked done."""
    good = [_msg(n) for n in range(5)]

    class BadPageWaha(FakeWaha):
        async def chat_messages(
            self, chat_id: str, limit: int, before_ts: int | None
        ) -> list[dict[str, Any]]:
            self.calls += 1
            everything = sorted(
                good + [BAD_TO_NULL], key=lambda m: m["timestamp"], reverse=True
            )
            page = [
                m
                for m in everything
                if before_ts is None or m["timestamp"] <= before_ts
            ]
            return page[:limit]

    waha = BadPageWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}], messages=good
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        written = await backfill_chat(
            conn, waha, "5511999@c.us", 30, page_size=10
        )
        cur = await conn.execute("SELECT count(*) FROM messages")
        assert (await cur.fetchone())[0] == 5
        cur = await conn.execute(
            "SELECT done FROM sync_state WHERE chat_id = %s",
            ("5511999@c.us",),
        )
        assert (await cur.fetchone())[0] is True
    assert written == 5


# ---------------------------------------------------------------------------
# Bug 2: overlapping passes / head-of-list starvation.
# ---------------------------------------------------------------------------


async def test_start_backfill_pass_refuses_second_pass_while_running(
    pool: AsyncConnectionPool,
) -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowWaha(FakeWaha):
        async def chats_overview(
            self, limit: int, offset: int
        ) -> list[dict[str, Any]]:
            started.set()
            await release.wait()
            return await super().chats_overview(limit, offset)

    waha = SlowWaha(
        chats=[{"id": "5511999@c.us", "name": "Fulano"}],
        messages=[_msg(0)],
    )
    assert backfill_in_progress() is False
    task1 = start_backfill_pass(pool, waha, 30)
    assert task1 is not None
    await started.wait()
    assert backfill_in_progress() is True

    task2 = start_backfill_pass(pool, waha, 30)
    assert task2 is None, "a second pass must not start while one is running"

    release.set()
    await task1
    assert backfill_in_progress() is False


async def test_backfill_all_visits_least_recently_attempted_first(
    pool: AsyncConnectionPool,
) -> None:
    """Fairness ordering: a chat that was never attempted, or was attempted
    longest ago, must be visited before a chat touched recently -- otherwise
    a pass that never finishes starves the tail of the chat list forever."""
    visit_order: list[str] = []

    class OrderTrackingWaha(FakeWaha):
        async def chat_messages(
            self, chat_id: str, limit: int, before_ts: int | None
        ) -> list[dict[str, Any]]:
            if chat_id not in visit_order:
                visit_order.append(chat_id)
            return await super().chat_messages(chat_id, limit, before_ts)

    waha = OrderTrackingWaha(
        chats=[
            {"id": "a@c.us", "name": "A"},  # touched recently
            {"id": "b@c.us", "name": "B"},  # never attempted
            {"id": "c@c.us", "name": "C"},  # touched long ago
        ],
        messages=[],
    )
    async with pool.connection() as conn:
        await upsert_chat(conn, "a@c.us", "A", None)
        await upsert_chat(conn, "c@c.us", "C", None)
        await conn.execute(
            "INSERT INTO sync_state (chat_id, done, updated_at) "
            "VALUES (%s, true, now())",
            ("a@c.us",),
        )
        await conn.execute(
            "INSERT INTO sync_state (chat_id, done, updated_at) "
            "VALUES (%s, true, now() - interval '2 hours')",
            ("c@c.us",),
        )

    await backfill_all(pool, waha, 30)
    assert visit_order == ["b@c.us", "c@c.us", "a@c.us"]


async def test_backfill_status_reports_running_and_oldest_updated_at(
    pool: AsyncConnectionPool,
) -> None:
    """GET /api/backfill/status: exercised at the sync-module level since the
    fields it must expose (backfill_in_progress, sync_state.updated_at) are
    plain functions/columns, not FastAPI-specific."""
    assert backfill_in_progress() is False
    async with pool.connection() as conn:
        await upsert_chat(conn, "5511999@c.us", "Fulano", None)
        await conn.execute(
            "INSERT INTO sync_state (chat_id, done, updated_at) "
            "VALUES (%s, true, now() - interval '1 hour')",
            ("5511999@c.us",),
        )
        cur = await conn.execute("SELECT min(updated_at) FROM sync_state")
        oldest = (await cur.fetchone())[0]
        assert oldest is not None
