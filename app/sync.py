import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Json

log = logging.getLogger(__name__)


# A jid always ends in one of these; a chat id never contains "_".
_JID_SUFFIXES = ("@c.us", "@g.us", "@lid", "@s.whatsapp.net", "@broadcast", "@newsletter")


def _is_jid(value: Any) -> bool:
    return isinstance(value, str) and value.endswith(_JID_SUFFIXES)


def _id_parts(message_id: str | None) -> tuple[str, bool] | None:
    """Recover (chat_id, from_me) from a WAHA message id.

    WAHA/whatsapp-web.js compose ids as "{fromMe}_{chatId}_{serial}" for DMs
    and "{fromMe}_{chatId}_{serial}_{participant}" for groups. The second
    segment is the chat in both directions and both chat types, which the
    from/to fields are not: for a group WAHA puts the group in `from` and the
    participant in `to` even for the owner's own messages, and for an outgoing
    DM `to` is null. The serial may itself contain "_", so split on every "_"
    and trust segment 1 only if it looks like a jid.
    """
    parts = (message_id or "").split("_")
    if len(parts) < 3 or parts[0] not in ("true", "false") or not _is_jid(parts[1]):
        return None
    return parts[1], parts[0] == "true"


def chat_id_of(payload: dict[str, Any]) -> str | None:
    """Derive the chat id a message belongs to, from its id (authoritative).

    Falls back to the old from/to heuristic when the id is absent or doesn't
    parse. That fallback can still yield None (WAHA has delivered payloads
    where the field is present but null) -- callers decide how to handle a
    message they can't place in a chat (see upsert_message).
    """
    parsed = _id_parts(payload.get("id"))
    if parsed is not None:
        return parsed[0]
    return payload.get("to") if payload.get("fromMe") else payload.get("from")


def _ts(seconds: Any) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def parse_message(
    payload: dict[str, Any],
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(tz=timezone.utc)
    chat_id = chat_id_of(payload)
    media = payload.get("media") or {}
    has_media = bool(payload.get("hasMedia")) and bool(media.get("url"))
    reply_to = payload.get("replyTo") or {}
    return {
        "id": payload["id"],
        "chat_id": chat_id,
        "ts": _ts(payload["timestamp"]),
        "from_me": bool(payload.get("fromMe")),
        # In a group the human sender is `participant` (set in both
        # directions). In a DM it's `from` for incoming; for outgoing `from`
        # is the *contact* (the chat), never the owner, so leave it null
        # rather than attribute the owner's message to the contact.
        "sender_jid": payload.get("participant")
        or (None if payload.get("fromMe") else payload.get("from")),
        "body": payload.get("body"),
        "type": payload.get("type") or ("media" if has_media else "chat"),
        "has_media": has_media,
        "media_url": media.get("url"),
        "media_mime": media.get("mimetype"),
        "media_name": media.get("filename"),
        "media_expires_at": (
            now + timedelta(days=retention_days) if has_media else None
        ),
        "ack": payload.get("ack"),
        "reply_to_id": reply_to.get("id"),
        "raw": payload,
    }


async def upsert_chat(
    conn: AsyncConnection,
    chat_id: str,
    name: str | None,
    last_msg_ts: datetime | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO chats (id, name, is_group, last_msg_ts)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
          name = COALESCE(EXCLUDED.name, chats.name),
          last_msg_ts = GREATEST(
            COALESCE(EXCLUDED.last_msg_ts, chats.last_msg_ts),
            COALESCE(chats.last_msg_ts, EXCLUDED.last_msg_ts)
          ),
          updated_at = now()
        """,
        (chat_id, name, chat_id.endswith("@g.us"), last_msg_ts),
    )


_STUB_RAW = "{}"  # sentinel marking a placeholder row created by a side-channel
# event (ack/edited/revoked/reaction) that arrived before message.any. A real
# message's raw is always the full webhook payload, never an empty object.


async def upsert_message(
    conn: AsyncConnection, payload: dict[str, Any], retention_days: int
) -> bool:
    row = parse_message(payload, retention_days)
    if not row["chat_id"]:
        # Root cause of the crash-every-pass bug: WAHA sometimes delivers a
        # message whose derived chat id (payload.to/from) is null. Skip just
        # this message rather than letting AttributeError blow up the whole
        # page/chat walk (backfill) or the webhook request. Shared by both
        # paths since both route through upsert_message.
        log.warning(
            "skipping message %s: no chat_id derivable from payload",
            row.get("id"),
        )
        return False
    await upsert_chat(conn, row["chat_id"], None, row["ts"])
    cur = await conn.execute(
        """
        INSERT INTO messages (
          id, chat_id, ts, from_me, sender_jid, body, type, has_media,
          media_url, media_mime, media_name, media_expires_at, ack,
          reply_to_id, raw
        ) VALUES (
          %(id)s, %(chat_id)s, %(ts)s, %(from_me)s, %(sender_jid)s, %(body)s,
          %(type)s, %(has_media)s, %(media_url)s, %(media_mime)s,
          %(media_name)s, %(media_expires_at)s, %(ack)s, %(reply_to_id)s,
          %(raw)s
        )
        ON CONFLICT (id) DO UPDATE SET
          chat_id = EXCLUDED.chat_id,
          ts = EXCLUDED.ts,
          from_me = EXCLUDED.from_me,
          sender_jid = EXCLUDED.sender_jid,
          body = CASE WHEN messages.edited_at IS NULL
                      THEN EXCLUDED.body ELSE messages.body END,
          type = EXCLUDED.type,
          has_media = EXCLUDED.has_media,
          media_url = EXCLUDED.media_url,
          media_mime = EXCLUDED.media_mime,
          media_name = EXCLUDED.media_name,
          media_expires_at = EXCLUDED.media_expires_at,
          ack = COALESCE(messages.ack, EXCLUDED.ack),
          reply_to_id = EXCLUDED.reply_to_id,
          raw = EXCLUDED.raw
        WHERE messages.raw = %(stub_raw)s::jsonb
        """,
        {**row, "raw": Json(row["raw"]), "stub_raw": _STUB_RAW},
    )
    return cur.rowcount == 1


async def _ensure_stub(conn: AsyncConnection, message_id: str | None) -> bool:
    """Make sure a row exists for message_id so a side-channel UPDATE has
    something to land on, even if message.any hasn't arrived yet.

    The id is the only chat_id a bare ack/edited/revoked/reaction payload
    carries. Returns False (no-op) when chat_id can't be derived from it: we
    cannot satisfy messages.chat_id (NOT NULL, FK to chats) and must not
    invent a chat, so the caller treats that as an unprocessable event.
    """
    parsed = _id_parts(message_id)
    if parsed is None:
        return False
    chat_id, from_me = parsed
    await upsert_chat(conn, chat_id, None, None)
    await conn.execute(
        """
        INSERT INTO messages (id, chat_id, ts, from_me, raw)
        VALUES (%s, %s, now(), %s, %s::jsonb)
        ON CONFLICT (id) DO NOTHING
        """,
        (message_id, chat_id, from_me, _STUB_RAW),
    )
    return True


async def _resolve_stored_message_id(
    conn: AsyncConnection, candidate: str | None
) -> str | None:
    """Resolve GOWS' raw edit/revoke target to our serialized message id.

    Ordinary side-channel events carry the serialized id directly. Current
    GOWS edit/revoke events instead carry only the WhatsApp raw id in
    editedMessageId/revokedMessageId, while the archive primary key also
    includes direction, chat, and sometimes participant.
    """
    if not candidate:
        return None
    cur = await conn.execute(
        """
        SELECT id
        FROM messages
        WHERE id = %(candidate)s
           OR raw #>> '{_data,Info,ID}' = %(candidate)s
           OR id LIKE %(ending)s ESCAPE '\\'
           OR id LIKE %(middle)s ESCAPE '\\'
        ORDER BY (id = %(candidate)s) DESC
        LIMIT 1
        """,
        {
            "candidate": candidate,
            "ending": "%\\_" + candidate.replace("%", "\\%").replace("_", "\\_"),
            "middle": "%\\_"
            + candidate.replace("%", "\\%").replace("_", "\\_")
            + "\\_%",
        },
    )
    row = await cur.fetchone()
    if row is not None:
        return row[0]
    # A serialized id can safely become a stub if message.any is merely late.
    return candidate if _id_parts(candidate) is not None else None


def verify_hmac(body: bytes, header: str | None, key: str) -> bool:
    if not header:
        return False
    expected = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header.strip().lower())


async def handle_event(
    conn: AsyncConnection,
    event: str,
    payload: dict[str, Any],
    retention_days: int,
    event_time: datetime | None = None,
) -> None:
    event_time = event_time or datetime.now(timezone.utc)
    if event in ("message", "message.any"):
        await upsert_message(conn, payload, retention_days)

    elif event == "message.ack":
        target = payload.get("id")
        ack = payload.get("ack")
        cur = await conn.execute(
            "UPDATE messages SET ack = %s WHERE id = %s", (ack, target)
        )
        if cur.rowcount == 0 and await _ensure_stub(conn, target):
            await conn.execute(
                "UPDATE messages SET ack = %s WHERE id = %s", (ack, target)
            )

    elif event == "message.edited":
        candidate = payload.get("editedMessageId") or payload.get("id")
        target = await _resolve_stored_message_id(conn, candidate)
        if target is None:
            return
        body = payload.get("body")
        cur = await conn.execute(
            "UPDATE messages SET body = %s, edited_at = %s WHERE id = %s",
            (body, event_time, target),
        )
        if cur.rowcount == 0 and await _ensure_stub(conn, target):
            await conn.execute(
                "UPDATE messages SET body = %s, edited_at = %s WHERE id = %s",
                (body, event_time, target),
            )

    elif event == "message.revoked":
        before = payload.get("before") or {}
        candidate = (
            payload.get("revokedMessageId")
            or (before.get("id") if isinstance(before, dict) else None)
            or payload.get("id")
        )
        target = await _resolve_stored_message_id(conn, candidate)
        if target is None:
            return
        cur = await conn.execute(
            "UPDATE messages SET revoked_at = %s WHERE id = %s",
            (event_time, target),
        )
        if cur.rowcount == 0 and await _ensure_stub(conn, target):
            await conn.execute(
                "UPDATE messages SET revoked_at = %s WHERE id = %s",
                (event_time, target),
            )

    elif event == "message.reaction":
        reaction = payload.get("reaction") or {}
        target = reaction.get("messageId")
        frm = payload.get("from")
        text = reaction.get("text")

        cur = await conn.execute(
            "SELECT reactions FROM messages WHERE id = %s FOR UPDATE", (target,)
        )
        row = await cur.fetchone()
        if row is None:
            if not await _ensure_stub(conn, target):
                return
            reactions: list[dict[str, Any]] = []
        else:
            reactions = list(row[0] or [])

        # Replace (not append) so redelivery of the same reaction is a
        # no-op, and an empty text is a removal rather than a tombstone
        # entry (WhatsApp reactions are "one per sender per message").
        reactions = [r for r in reactions if r.get("from") != frm]
        if text:
            reactions.append({"from": frm, "text": text})

        await conn.execute(
            "UPDATE messages SET reactions = %s::jsonb WHERE id = %s",
            (json.dumps(reactions), target),
        )
    # session.status and anything unrecognized: logged by the route, no row


async def _apply_page(
    conn: AsyncConnection,
    page: list[dict[str, Any]],
    retention_days: int,
    floor_ts: int | None,
) -> tuple[int, int | None]:
    """Upsert every message in a page. Returns (written, oldest_ts_seen)."""
    written = 0
    oldest = floor_ts
    for msg in page:
        if await upsert_message(conn, msg, retention_days):
            written += 1
        ts = int(msg["timestamp"])
        oldest = ts if oldest is None else min(oldest, ts)
    return written, oldest


async def _converge_top(
    conn: AsyncConnection,
    waha: Any,
    chat_id: str,
    retention_days: int,
    page_size: int,
) -> int:
    """Page from now() downward until hitting a message we already have.

    Catches whatever a missed webhook delivery left out since the last pass
    (finding I4). Doesn't touch sync_state: cursor_ts/done describe the deep
    walk below, not this top-up.
    """
    written = 0
    before_ts: int | None = None
    while True:
        page = await waha.chat_messages(chat_id, page_size, before_ts)
        if not page:
            return written
        page_written, oldest = await _apply_page(
            conn, page, retention_days, before_ts
        )
        written += page_written
        # ponytail: stops at the first already-known message in a page
        # rather than a binary-searched exact boundary; fine because a
        # missed gap is small and re-scanning a page or two is cheap.
        hit_known = page_written < len(page)
        before_ts = oldest
        if hit_known or len(page) < page_size:
            return written


async def backfill_chat(
    conn: AsyncConnection,
    waha: Any,
    chat_id: str,
    retention_days: int,
    page_size: int = 200,
) -> int:
    cur = await conn.execute(
        "SELECT cursor_ts, done FROM sync_state WHERE chat_id = %s", (chat_id,)
    )
    row = await cur.fetchone()
    # GOWS keeps streaming older history into WAHA's own store for a long
    # time after pairing (hours, sometimes longer) and never emits a "history
    # sync done" event. A chat that finished its first walk minutes after
    # pairing only saw WAHA's most-recent messages at the time; everything
    # GOWS delivered afterwards is invisible unless we keep asking. So
    # done=true no longer means "this chat is finished forever" -- it means
    # "the deep walk hit an empty page last time we checked". Every pass
    # still attempts to go deeper; when there's truly nothing older that
    # costs exactly one request that comes back empty.
    was_done = bool(row) and row[1]
    stored_cursor = int(row[0].timestamp()) if row and row[0] is not None else None

    written = 0
    if was_done:
        # Converge at the top first: recovers anything a missed webhook left
        # out since the last pass.
        written += await _converge_top(
            conn, waha, chat_id, retention_days, page_size
        )

    # Continue the deep walk from the oldest message already held.
    # cursor_ts is inclusive (filter.timestamp.lte upstream), so a pass
    # resuming a chat that was already "done" steps one second past it --
    # otherwise every "nothing older" pass would re-fetch our own boundary
    # message and trip the tie-guard below instead of getting a clean
    # empty page. A chat resuming mid-walk after a crash (done=false,
    # cursor_ts set by the interrupted pass) keeps the exact original
    # cursor: it must retry at the same boundary in case that pass left an
    # unresolved tie there.
    before_ts = (
        stored_cursor - 1 if was_done and stored_cursor is not None
        else stored_cursor
    )
    final_cursor = stored_cursor

    while True:
        page = await waha.chat_messages(chat_id, page_size, before_ts)
        if not page:
            break

        page_written, oldest = await _apply_page(
            conn, page, retention_days, before_ts
        )
        written += page_written
        page_capacity = page_size

        if oldest == before_ts:
            # boundary tie: more than page_size messages share one timestamp.
            # Retry once with a bigger page to try to clear the tied block
            # instead of declaring the chat done with a hidden gap.
            retry_size = page_size * 2
            retry_page = await waha.chat_messages(chat_id, retry_size, before_ts)
            retry_written, retry_oldest = await _apply_page(
                conn, retry_page, retention_days, before_ts
            )
            written += retry_written
            if retry_oldest == before_ts:
                log.warning(
                    "backfill stuck: chat=%s has more than %d messages tied "
                    "at ts=%s; will retry on the next pass",
                    chat_id,
                    retry_size,
                    before_ts,
                )
                await conn.commit()
                return written
            page, oldest, page_capacity = retry_page, retry_oldest, retry_size

        before_ts = oldest
        final_cursor = oldest

        await conn.execute(
            """
            INSERT INTO sync_state (chat_id, cursor_ts, done)
            VALUES (%s, %s, false)
            ON CONFLICT (chat_id) DO UPDATE SET
              cursor_ts = EXCLUDED.cursor_ts, updated_at = now()
            """,
            (chat_id, datetime.fromtimestamp(final_cursor, tz=timezone.utc)),
        )
        # Commit each page's cursor immediately so a crash mid-chat resumes
        # from the last completed page instead of restarting the chat.
        await conn.commit()

        if len(page) < page_capacity:
            break

    await conn.execute(
        """
        INSERT INTO sync_state (chat_id, cursor_ts, done)
        VALUES (%s, %s, true)
        ON CONFLICT (chat_id) DO UPDATE SET done = true, updated_at = now()
        """,
        (
            chat_id,
            datetime.fromtimestamp(final_cursor, tz=timezone.utc)
            if final_cursor
            else None,
        ),
    )
    await conn.commit()
    return written


async def backfill_all(pool: Any, waha: Any, retention_days: int) -> dict[str, int]:
    # Fetch the whole chat list before ordering it: a chat with no
    # sync_state row at all has never been attempted, which is "staler than
    # stale" -- NULLS FIRST below puts it ahead of every chat that has been
    # touched at least once, however long ago. This makes progress durable
    # across interrupted/overlapping passes: whatever a pass touched has an
    # advanced updated_at, so the next pass naturally continues into the
    # untouched tail instead of restarting at the head. It does NOT
    # guarantee every chat is visited within a given pass, or that a chat
    # visited this pass won't be revisited before some other chat that
    # started even staler but sorts after it once this pass bumps it.
    raw_chats: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await waha.chats_overview(100, offset)
        if not page:
            break
        raw_chats.extend(page)
        offset += len(page)

    by_id = {c["id"]: c for c in raw_chats if c.get("id")}
    chat_ids = list(by_id.keys())

    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT c.id
            FROM unnest(%s::text[]) AS c(id)
            LEFT JOIN sync_state s ON s.chat_id = c.id
            ORDER BY s.updated_at ASC NULLS FIRST
            """,
            (chat_ids,),
        )
        ordered_ids = [r[0] for r in await cur.fetchall()]

    chats = 0
    messages = 0
    for chat_id in ordered_ids:
        chat = by_id[chat_id]
        chats += 1
        try:
            async with pool.connection() as conn:
                await upsert_chat(conn, chat_id, chat.get("name"), None)
                messages += await backfill_chat(
                    conn, waha, chat_id, retention_days
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill failed for %s: %r", chat_id, exc)
    return {"chats": chats, "messages": messages}


# ponytail: process-wide single-flight via a module-level task reference, no
# Redis/queue/DB advisory lock. Correct for this single-instance deployment;
# a second app instance would still race. Upgrade to a Postgres advisory
# lock if this ever runs behind more than one process.
_current_pass: "asyncio.Task[dict[str, int]] | None" = None


def backfill_in_progress() -> bool:
    return _current_pass is not None and not _current_pass.done()


def start_backfill_pass(
    pool: Any, waha: Any, retention_days: int
) -> "asyncio.Task[dict[str, int]] | None":
    """Start a backfill pass unless one is already running process-wide.

    Returns the new task, or None if a pass is already in flight. Holding
    the task in a module-level variable both keeps it from being garbage
    collected mid-pass and gives us something to check concurrency against.
    """
    global _current_pass
    if backfill_in_progress():
        return None
    _current_pass = asyncio.create_task(backfill_all(pool, waha, retention_days))
    return _current_pass


async def cancel_backfill_pass() -> None:
    """Cancel and release the process-wide pass during application shutdown."""
    global _current_pass
    task = _current_pass
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    _current_pass = None


async def _backfill_tick(pool: Any, waha: Any, retention_days: int) -> None:
    task = start_backfill_pass(pool, waha, retention_days)
    if task is None:
        log.info("backfill loop: pass already running, skipping this tick")
        return
    try:
        result = await task
        log.info("backfill pass: %s", result)
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill pass failed: %r", exc)


async def backfill_loop(pool: Any, waha: Any, retention_days: int) -> None:
    # GOWS emits no "history sync finished" event, so we re-pass on a timer.
    # Frequent for the first day while history streams in, then daily.
    for delay in [3600] * 24:
        await _backfill_tick(pool, waha, retention_days)
        await asyncio.sleep(delay)
    while True:
        await _backfill_tick(pool, waha, retention_days)
        await asyncio.sleep(86400)
