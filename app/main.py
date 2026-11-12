import asyncio
import base64
import json as _json
import logging
from asyncio import Queue
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.types.json import Json
from pydantic import BaseModel

from app.config import settings
from app.db import close_db, get_pool, init_db
from app.sync import (
    backfill_in_progress,
    backfill_loop,
    cancel_backfill_pass,
    handle_event,
    start_backfill_pass,
    verify_hmac,
)
from app.send_guard import (
    assert_can_mutate_message,
    assert_can_send,
    record_message_action,
    record_send,
)
from app.waha import Waha, WahaError

log = logging.getLogger(__name__)

waha = Waha(settings.waha_url, settings.waha_api_key, settings.waha_session)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

WAHA_UNAVAILABLE = "waha unavailable"
OUTCOME_UNKNOWN = "waha unavailable; outcome unknown; check chat before retrying"


LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


async def require_local(request: Request) -> None:
    """No key: the port is bound to localhost. What is left to stop is a web
    page in the owner's own browser reaching it — a foreign Host means DNS
    rebinding, a foreign Origin means a cross-site request."""
    host = request.headers.get("host", "")
    if host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="local access only")
    origin = request.headers.get("origin")
    if origin is not None and origin != f"http://{host}":
        raise HTTPException(status_code=403, detail="cross-origin request")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.hook_hmac_key == "dev":
        raise RuntimeError(
            "WHATSAPP_HOOK_HMAC_KEY must be set to a real secret, "
            "not the 'dev' default"
        )
    await init_db(settings.database_url)
    task = asyncio.create_task(
        backfill_loop(get_pool(), waha, settings.media_retention_days)
    )
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await cancel_backfill_pass()
        await waha.aclose()
        await close_db()


app = FastAPI(title="zap-viewer", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/session/status", dependencies=[Depends(require_local)])
async def session_status() -> dict[str, Any]:
    try:
        return await waha.status()
    except WahaError as exc:
        log.warning("waha status failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc


@app.post("/api/session/start", dependencies=[Depends(require_local)])
async def session_start() -> dict[str, Any]:
    try:
        return await waha.start()
    except WahaError as exc:
        log.warning("waha start failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc


@app.post("/api/session/stop", dependencies=[Depends(require_local)])
async def session_stop() -> dict[str, Any]:
    try:
        return await waha.stop()
    except WahaError as exc:
        log.warning("waha stop failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc


@app.get("/pair")
async def pair() -> FileResponse:
    return FileResponse(WEB_DIR / "pair.html")


@app.get("/api/session/qr", dependencies=[Depends(require_local)])
async def session_qr() -> Response:
    try:
        return Response(content=await waha.qr(), media_type="image/png")
    except WahaError as exc:
        log.warning("waha qr failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc


_subscribers: set[Queue] = set()


async def broadcast(event: str, payload: dict) -> None:
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait({"event": event, "payload": payload})
        except Exception:  # noqa: BLE001
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


@app.get("/api/events", dependencies=[Depends(require_local)])
async def events() -> StreamingResponse:
    q: Queue = Queue(maxsize=100)
    _subscribers.add(q)

    async def stream():
        try:
            while True:
                item = await q.get()
                yield f"data: {_json.dumps(item)}\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class SendText(BaseModel):
    chat_id: str
    text: str
    confirm_name: str
    allow_group: bool = False
    allow_ambiguous: bool = False
    reply_to: str | None = None


class SendSeen(BaseModel):
    chat_id: str
    message_ids: list[str] | None = None


class EditMessageRequest(BaseModel):
    text: str
    confirm_name: str
    allow_group: bool = False
    allow_ambiguous: bool = False


class DeleteMessageRequest(BaseModel):
    confirm_name: str
    confirm_message_id: str
    allow_group: bool = False
    allow_ambiguous: bool = False


def reject_oversized(request: Request) -> None:
    # Content-Length é declarado pelo cliente, não pode ser a única defesa,
    # mas evita ler um upload já anunciado como grande demais para a memória
    # antes mesmo de validar os campos do formulário.
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file over 64 MB")


@app.post("/api/send/text", dependencies=[Depends(require_local)])
async def send_text(body: SendText) -> dict:
    async with get_pool().connection() as conn:
        try:
            await assert_can_send(
                conn,
                body.chat_id,
                body.confirm_name,
                body.allow_group,
                body.allow_ambiguous,
            )
        except HTTPException as exc:
            # Recusas também ficam auditadas; são justamente o sinal de que a
            # resolução do destinatário precisa ser corrigida antes de tentar.
            await record_send(conn, body.chat_id, body.text, False, str(exc.detail))
            raise
        try:
            # ponytail: sem teto de taxa e sem trava — envios concorrentes só
            # disputam as 10 conexões do pool (app/db.py:12), e cada um segura
            # a sua pelos 120s de read timeout do WAHA (app/waha.py:30). Se
            # muitos envios simultâneos travarem a leitura do arquivo, voltar
            # com um lock por chat.
            result = await waha.send_text(body.chat_id, body.text, body.reply_to)
        # Amplo de propósito: depois que a requisição saiu, falha de transporte
        # não prova que a mensagem deixou de ser entregue.
        except Exception as exc:  # noqa: BLE001
            log.warning("waha send_text failed: %r", exc)
            await record_send(
                conn, body.chat_id, body.text, False, str(exc), uncertain=True
            )
            raise HTTPException(status_code=503, detail=OUTCOME_UNKNOWN) from exc
        await record_send(conn, body.chat_id, body.text, True)
        return result


@app.post(
    "/api/send/media",
    dependencies=[Depends(require_local), Depends(reject_oversized)],
)
async def send_media(
    chat_id: str = Form(...),
    confirm_name: str = Form(...),
    allow_group: bool = Form(default=False),
    allow_ambiguous: bool = Form(default=False),
    caption: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> dict:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file over 64 MB")

    filename = file.filename or "file"
    logged = f"[{filename}] {caption or ''}".strip()

    async with get_pool().connection() as conn:
        try:
            await assert_can_send(
                conn, chat_id, confirm_name, allow_group, allow_ambiguous
            )
        except HTTPException as exc:
            await record_send(conn, chat_id, logged, False, str(exc.detail))
            raise
        try:
            result = await waha.send_media(
                chat_id,
                filename,
                file.content_type or "application/octet-stream",
                base64.b64encode(data).decode(),
                caption,
            )
        except Exception as exc:  # noqa: BLE001 — ver send_text
            log.warning("waha send_media failed: %r", exc)
            await record_send(
                conn, chat_id, logged, False, str(exc), uncertain=True
            )
            raise HTTPException(status_code=503, detail=OUTCOME_UNKNOWN) from exc
        await record_send(conn, chat_id, logged, True)
        return result


@app.post("/api/send/seen", dependencies=[Depends(require_local)])
async def send_seen(body: SendSeen) -> dict:
    try:
        return await waha.send_seen(body.chat_id, body.message_ids)
    except WahaError as exc:
        log.warning("waha send_seen failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc


@app.put(
    "/api/chats/{chat_id}/messages/{message_id}",
    dependencies=[Depends(require_local)],
)
async def edit_message(
    chat_id: str, message_id: str, body: EditMessageRequest
) -> dict[str, Any]:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")

    async with get_pool().connection() as conn:
        previous: str | None = None
        try:
            target = await assert_can_mutate_message(
                conn,
                chat_id,
                message_id,
                body.confirm_name,
                body.allow_group,
                body.allow_ambiguous,
                "edit",
            )
            previous = target.body
        except HTTPException as exc:
            await record_message_action(
                conn,
                "edit",
                chat_id,
                message_id,
                previous,
                body.text,
                False,
                str(exc.detail),
            )
            raise

        try:
            result = await waha.edit_message(chat_id, message_id, body.text)
        except Exception as exc:  # noqa: BLE001
            log.warning("waha edit_message failed: %r", exc)
            await record_message_action(
                conn,
                "edit",
                chat_id,
                message_id,
                previous,
                body.text,
                False,
                str(exc),
                uncertain=True,
            )
            raise HTTPException(status_code=503, detail=OUTCOME_UNKNOWN) from exc

        await record_message_action(
            conn, "edit", chat_id, message_id, previous, body.text, True
        )
        return {"result": result, "verification": "pending_webhook"}


@app.delete(
    "/api/chats/{chat_id}/messages/{message_id}",
    dependencies=[Depends(require_local)],
)
async def delete_message(
    chat_id: str, message_id: str, body: DeleteMessageRequest
) -> dict[str, Any]:
    async with get_pool().connection() as conn:
        previous: str | None = None
        try:
            target = await assert_can_mutate_message(
                conn,
                chat_id,
                message_id,
                body.confirm_name,
                body.allow_group,
                body.allow_ambiguous,
                "delete",
            )
            previous = target.body
            if body.confirm_message_id != message_id:
                raise HTTPException(
                    status_code=409,
                    detail="confirm_message_id does not match the target message",
                )
        except HTTPException as exc:
            await record_message_action(
                conn,
                "delete",
                chat_id,
                message_id,
                previous,
                None,
                False,
                str(exc.detail),
            )
            raise

        try:
            result = await waha.delete_message(chat_id, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("waha delete_message failed: %r", exc)
            await record_message_action(
                conn,
                "delete",
                chat_id,
                message_id,
                previous,
                None,
                False,
                str(exc),
                uncertain=True,
            )
            raise HTTPException(status_code=503, detail=OUTCOME_UNKNOWN) from exc

        await record_message_action(
            conn, "delete", chat_id, message_id, previous, None, True
        )
        return {"result": result, "verification": "pending_webhook"}


@app.get("/api/message-actions", dependencies=[Depends(require_local)])
async def message_actions(
    chat_id: str | None = None,
    message_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, action, chat_id, message_id, previous_text, new_text,
                   ts, ok, error, uncertain
            FROM message_action_log
            WHERE (%(chat_id)s::text IS NULL OR chat_id = %(chat_id)s)
              AND (%(message_id)s::text IS NULL OR message_id = %(message_id)s)
            ORDER BY ts DESC
            LIMIT %(limit)s
            """,
            {"chat_id": chat_id, "message_id": message_id, "limit": limit},
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "action": r[1],
            "chat_id": r[2],
            "message_id": r[3],
            "previous_text": r[4],
            "new_text": r[5],
            "ts": r[6].isoformat(),
            "ok": r[7],
            "error": r[8],
            "uncertain": r[9],
        }
        for r in rows
    ]


@app.post("/webhook/waha")
async def webhook(request: Request) -> dict[str, str]:
    body = await request.body()
    signature = request.headers.get("x-webhook-hmac")
    if not verify_hmac(body, signature, settings.hook_hmac_key):
        raise HTTPException(status_code=401, detail="bad signature")

    pool = get_pool()
    event = ""
    payload: dict[str, Any] = {}
    try:
        data = _json.loads(body)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        event = data.get("event", "")
        payload = data.get("payload") or {}

        async with pool.connection() as conn:
            await handle_event(
                conn, event, payload, settings.media_retention_days
            )
            await conn.execute(
                "INSERT INTO webhook_log (event, payload) VALUES (%s, %s)",
                (event, Json(payload)),
            )
        await broadcast(event, payload)
    except Exception as exc:  # noqa: BLE001
        error = f"{exc!r} raw_body={body!r}"
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO webhook_log (event, payload, error) "
                "VALUES (%s, %s, %s)",
                (event, Json(payload), error),
            )
    return {"status": "ok"}


@app.post("/api/backfill/start", dependencies=[Depends(require_local)])
async def backfill_start() -> dict:
    task = start_backfill_pass(get_pool(), waha, settings.media_retention_days)
    if task is None:
        return {"status": "already_running"}
    return {"status": "started"}


@app.get("/api/chats", dependencies=[Depends(require_local)])
async def list_chats(
    limit: int = 100, offset: int = 0, q: str | None = None
) -> list[dict]:
    sql = """
        SELECT id, name, is_group, last_msg_ts, archived
        FROM chats
        WHERE (%(q)s::text IS NULL OR name ILIKE '%%' || %(q)s || '%%'
               OR id ILIKE '%%' || %(q)s || '%%')
        ORDER BY last_msg_ts DESC NULLS LAST
        LIMIT %(limit)s OFFSET %(offset)s
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(sql, {"q": q, "limit": limit, "offset": offset})
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "is_group": r[2],
            "last_msg_ts": r[3].isoformat() if r[3] else None,
            "archived": r[4],
        }
        for r in rows
    ]


@app.get(
    "/api/chats/{chat_id}/messages", dependencies=[Depends(require_local)]
)
async def list_messages(
    chat_id: str, limit: int = 50, before_ts: str | None = None
) -> list[dict]:
    sql = """
        SELECT id, ts, from_me, sender_jid, body, type, has_media,
               media_mime, media_name, media_expires_at, ack, reply_to_id,
               reactions, edited_at, revoked_at, media_url
        FROM messages
        WHERE chat_id = %(chat_id)s
          AND (%(before_ts)s::text IS NULL
               OR ts < %(before_ts)s::timestamptz)
          AND raw <> '{}'::jsonb
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            sql, {"chat_id": chat_id, "before_ts": before_ts, "limit": limit}
        )
        rows = await cur.fetchall()
    return [_message_row(r) for r in rows]


def _is_archived(media_url: str | None) -> bool:
    """Media written before R2 was switched on lived in WAHA's container-local
    /tmp and did not survive a restart. Those rows are gone for good, which is
    a different fact from "the R2 lifecycle rule deleted it" and must not be
    reported as expiry."""
    return bool(media_url) and "/api/s3/" in media_url


def _message_row(r: tuple) -> dict:
    expires = r[9]
    return {
        "id": r[0],
        "ts": r[1].isoformat(),
        "from_me": r[2],
        "sender_jid": r[3],
        "body": r[4],
        "type": r[5],
        "has_media": r[6],
        "media_mime": r[7],
        "media_name": r[8],
        "media_expires_at": expires.isoformat() if expires else None,
        "media_url": f"/api/media/{r[0]}" if r[6] else None,
        "media_archived": _is_archived(r[15]) if r[6] else None,
        "ack": r[10],
        "reply_to_id": r[11],
        "reactions": r[12],
        "edited_at": r[13].isoformat() if r[13] else None,
        "revoked_at": r[14].isoformat() if r[14] else None,
    }


@app.get("/api/search", dependencies=[Depends(require_local)])
async def search(
    q: str, chat_id: str | None = None, limit: int = 50
) -> list[dict]:
    sql = """
        SELECT m.id, m.chat_id, m.ts, m.from_me, m.body,
               ts_headline('portuguese', m.body,
                           websearch_to_tsquery('portuguese', %(q)s),
                           'StartSel=<<HL>>, StopSel=<</HL>>') AS hl,
               c.name
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.search_vec @@ websearch_to_tsquery('portuguese', %(q)s)
          AND (%(chat_id)s::text IS NULL OR m.chat_id = %(chat_id)s)
        ORDER BY m.ts DESC
        LIMIT %(limit)s
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            sql, {"q": q, "chat_id": chat_id, "limit": limit}
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "chat_name": r[6],
            "ts": r[2].isoformat(),
            "from_me": r[3],
            "body": r[4],
            "highlight": r[5],
        }
        for r in rows
    ]


@app.get("/api/backfill/status", dependencies=[Depends(require_local)])
async def backfill_status() -> dict:
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            """
            SELECT
              (SELECT count(*) FROM chats),
              (SELECT count(*) FROM sync_state WHERE done),
              (SELECT min(cursor_ts) FROM sync_state),
              (SELECT min(updated_at) FROM sync_state),
              (SELECT count(*) FROM messages)
            """
        )
        chats, done, oldest, oldest_updated, messages = await cur.fetchone()
    return {
        "chats": chats,
        "chats_done": done,
        "oldest_cursor": oldest.isoformat() if oldest else None,
        "messages": messages,
        "running": backfill_in_progress(),
        # How stale the least-recently-attempted chat is. Only reflects
        # chats that have a sync_state row at all -- a chat never attempted
        # once (no row yet) doesn't show up here even though it's the
        # stalest possible case; see backfill_all's fairness ordering.
        "oldest_updated_at": (
            oldest_updated.isoformat() if oldest_updated else None
        ),
    }


_SAFE_MEDIA_PREFIXES = ("image/", "video/", "audio/")
# image/svg+xml is a scriptable document (<svg><script>...) despite the
# image/ prefix; it must never round-trip or it executes on the API origin
# same as text/html would (stored XSS, see finding C1).
_UNSAFE_MEDIA_TYPES = ("image/svg+xml",)


def _safe_media_type(mime: str | None) -> str:
    """Allowlist what we tell the browser to render.

    A blob URL for content the API serves inherits the API's origin, so an
    attacker-controlled media_mime like "text/html" must never round-trip:
    that would execute sender-supplied HTML on the same origin as the API
    key in localStorage (stored XSS, see finding C1).
    """
    mt = (mime or "").split(";")[0].strip().lower()
    if mt in _UNSAFE_MEDIA_TYPES:
        return "application/octet-stream"
    if mt.startswith(_SAFE_MEDIA_PREFIXES) or mt == "application/pdf":
        return mt
    return "application/octet-stream"


@app.get("/api/media/{message_id}", dependencies=[Depends(require_local)])
async def media(message_id: str) -> Response:
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT media_url, media_mime, media_name, media_expires_at "
            "FROM messages WHERE id = %s AND has_media",
            (message_id,),
        )
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="no media for this message")

    url, mime, name, expires = row
    if expires and expires < datetime.now(tz=timezone.utc):
        raise HTTPException(
            status_code=410,
            detail=f"media expired ({settings.media_retention_days}d)",
        )

    try:
        upstream = await waha.fetch_media(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("waha fetch_media failed: %r", exc)
        raise HTTPException(status_code=503, detail=WAHA_UNAVAILABLE) from exc

    if upstream.status_code in (404, 410):
        raise HTTPException(
            status_code=410,
            detail=f"media expired ({settings.media_retention_days}d)",
        )
    if upstream.status_code >= 400:
        raise HTTPException(status_code=502, detail="waha error")

    headers = {"X-Content-Type-Options": "nosniff"}
    if name:
        headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(name)}"
    return Response(
        content=upstream.content,
        media_type=_safe_media_type(mime or upstream.headers.get("content-type")),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# AI summary storage + endpoints
# ---------------------------------------------------------------------------


class SummaryUpsert(BaseModel):
    summary: str
    covered_through_ts: datetime
    covered_message_count: int
    model: str | None = None


def _summary_row(r: tuple) -> dict:
    return {
        "chat_id": r[0],
        "summary": r[1],
        "covered_through_ts": r[2].isoformat(),
        "covered_message_count": r[3],
        "model": r[4],
        "updated_at": r[5].isoformat(),
    }


@app.get("/api/summaries", dependencies=[Depends(require_local)])
async def list_summaries(
    limit: int = 50, offset: int = 0, only_stale: bool = False
) -> list[dict]:
    sql = """
        WITH summarized AS (
            SELECT s.chat_id, c.name AS chat_name, c.is_group, s.summary,
                   s.covered_through_ts, s.updated_at, c.last_msg_ts,
                   (SELECT count(*) FROM messages m
                    WHERE m.chat_id = s.chat_id
                      AND m.ts > s.covered_through_ts) AS pending_count
            FROM chat_summaries s
            JOIN chats c ON c.id = s.chat_id
        )
        SELECT chat_id, chat_name, is_group, summary, covered_through_ts,
               updated_at, pending_count, last_msg_ts
        FROM summarized
        WHERE (NOT %(only_stale)s OR pending_count > 0)
        ORDER BY pending_count DESC, last_msg_ts DESC NULLS LAST
        LIMIT %(limit)s OFFSET %(offset)s
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            sql,
            {"only_stale": only_stale, "limit": limit, "offset": offset},
        )
        rows = await cur.fetchall()
    return [
        {
            "chat_id": r[0],
            "chat_name": r[1],
            "is_group": r[2],
            "summary": r[3],
            "covered_through_ts": r[4].isoformat(),
            "updated_at": r[5].isoformat(),
            "pending_count": r[6],
            "last_msg_ts": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


@app.get("/api/summaries/pending", dependencies=[Depends(require_local)])
async def list_pending_summaries(
    limit: int = 20, min_pending: int = 1
) -> list[dict]:
    sql = """
        WITH candidates AS (
            SELECT c.id AS chat_id, c.name AS chat_name, c.last_msg_ts,
                   s.covered_through_ts,
                   (SELECT count(*) FROM messages m
                    WHERE m.chat_id = c.id
                      AND (s.covered_through_ts IS NULL
                           OR m.ts > s.covered_through_ts)) AS pending_count
            FROM chats c
            LEFT JOIN chat_summaries s ON s.chat_id = c.id
            WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.id)
        )
        SELECT chat_id, chat_name, pending_count, covered_through_ts, last_msg_ts
        FROM candidates
        WHERE covered_through_ts IS NULL OR pending_count >= %(min_pending)s
        ORDER BY pending_count DESC
        LIMIT %(limit)s
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            sql, {"min_pending": min_pending, "limit": limit}
        )
        rows = await cur.fetchall()
    return [
        {
            "chat_id": r[0],
            "chat_name": r[1],
            "pending_count": r[2],
            "covered_through_ts": r[3].isoformat() if r[3] else None,
            "last_msg_ts": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


@app.put("/api/summaries/{chat_id}", dependencies=[Depends(require_local)])
async def put_summary(chat_id: str, body: SummaryUpsert) -> dict:
    sql = """
        INSERT INTO chat_summaries
          (chat_id, summary, covered_through_ts, covered_message_count, model,
           updated_at)
        VALUES (%(chat_id)s, %(summary)s, %(covered_through_ts)s,
                %(covered_message_count)s, %(model)s, now())
        ON CONFLICT (chat_id) DO UPDATE SET
          summary = CASE
            WHEN EXCLUDED.covered_through_ts > chat_summaries.covered_through_ts
            THEN EXCLUDED.summary ELSE chat_summaries.summary END,
          covered_through_ts = GREATEST(
            EXCLUDED.covered_through_ts, chat_summaries.covered_through_ts
          ),
          covered_message_count = CASE
            WHEN EXCLUDED.covered_through_ts > chat_summaries.covered_through_ts
            THEN EXCLUDED.covered_message_count
            ELSE chat_summaries.covered_message_count END,
          model = CASE
            WHEN EXCLUDED.covered_through_ts > chat_summaries.covered_through_ts
            THEN EXCLUDED.model ELSE chat_summaries.model END,
          updated_at = now()
        RETURNING chat_id, summary, covered_through_ts, covered_message_count,
                  model, updated_at
    """
    async with get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM chats WHERE id = %s", (chat_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="chat not found")

        cur = await conn.execute(
            sql,
            {
                "chat_id": chat_id,
                "summary": body.summary,
                "covered_through_ts": body.covered_through_ts,
                "covered_message_count": body.covered_message_count,
                "model": body.model,
            },
        )
        row = await cur.fetchone()
    return _summary_row(row)


@app.get("/api/overview", dependencies=[Depends(require_local)])
async def overview() -> dict:
    try:
        status = await waha.status()
        me = status.get("me")
        session = {
            "status": status.get("status"),
            "me": me.get("id") if isinstance(me, dict) else me,
        }
    except WahaError as exc:
        log.warning("waha status failed: %r", exc)
        session = {"status": "UNREACHABLE", "me": None}

    async with get_pool().connection() as conn:
        cur = await conn.execute(
            """
            SELECT
              (SELECT count(*) FROM chats),
              (SELECT count(*) FROM sync_state WHERE done),
              (SELECT count(*) FROM messages),
              (SELECT min(ts) FROM messages)
            """
        )
        chats, chats_done, messages, oldest_ts = await cur.fetchone()

        cur = await conn.execute(
            """
            SELECT
              (SELECT count(*) FROM chat_summaries),
              (SELECT count(*) FROM chat_summaries s
               WHERE (SELECT count(*) FROM messages m
                      WHERE m.chat_id = s.chat_id
                        AND m.ts > s.covered_through_ts) > 0),
              (SELECT count(*) FROM chats c
               LEFT JOIN chat_summaries s ON s.chat_id = c.id
               WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.id)
                 AND (s.covered_through_ts IS NULL
                      OR (SELECT count(*) FROM messages m2
                          WHERE m2.chat_id = c.id
                            AND m2.ts > s.covered_through_ts) >= 1))
            """
        )
        total, stale, pending_chats = await cur.fetchone()

    return {
        "session": session,
        "backfill": {
            "chats": chats,
            "chats_done": chats_done,
            "messages": messages,
            "oldest_ts": oldest_ts.isoformat() if oldest_ts else None,
        },
        "summaries": {
            "total": total,
            "stale": stale,
            "pending_chats": pending_chats,
        },
    }
