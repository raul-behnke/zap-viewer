"""Guardas para enviar, editar e apagar mensagens.

O agente tem autorização para enviar sem passar por fila de rascunho. O risco
não é ele desobedecer, é ele obedecer com o destinatário errado: são centenas
de chats, com homônimos e grupos com nome de pessoa. Estas guardas transformam
"resolveu o nome errado" em erro visível antes de alterar o chat errado.

Instrução em markdown não é trava — o agente pode ignorá-la. Por isso a
verificação é aqui, no servidor.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from psycopg import AsyncConnection

EDIT_WINDOW = timedelta(minutes=15)
DELETE_WINDOW = timedelta(days=2)


@dataclass(frozen=True)
class MessageTarget:
    body: str | None
    ts: datetime
    message_type: str | None


def normalize_name(value: str | None) -> str:
    """Forma canônica para comparar nome de chat.

    Comparação crua recusaria "João" contra "joao" o tempo todo, e uma guarda
    que grita à toa é uma guarda que se aprende a contornar.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value)
    without_accent = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(without_accent.lower().split())


async def assert_can_send(
    conn: AsyncConnection,
    chat_id: str,
    confirm_name: str,
    allow_group: bool,
    allow_ambiguous: bool = False,
) -> None:
    """Recusa o envio quando o destinatário não confere.

    Levanta HTTPException; retorna None quando o envio pode seguir.
    """
    cur = await conn.execute(
        "SELECT name, is_group FROM chats WHERE id = %s", (chat_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown chat_id {chat_id!r}; it is not in the archive",
        )

    name, is_group = row
    # Chat sem nome é identificado só pelo JID, então o próprio id serve de
    # confirmação — não há outro rótulo que o agente pudesse conferir.
    expected = normalize_name(name) or normalize_name(chat_id)
    if normalize_name(confirm_name) != expected:
        raise HTTPException(
            status_code=409,
            detail=f"confirm_name does not match; this chat is {name or chat_id!r}",
        )

    if is_group and not allow_group:
        raise HTTPException(
            status_code=409,
            detail="this chat is a group; pass allow_group=true to send to it",
        )

    # O nome bater não basta: pode haver homônimos, e o agente pode ter
    # escolhido o chat_id errado entre eles sem perceber. Chat sem nome usa
    # o próprio id como confirmação, e id é único — não entra nesta checagem.
    if name is not None:
        cur = await conn.execute("SELECT id, name FROM chats WHERE name IS NOT NULL")
        rows = await cur.fetchall()
        matches = [cid for cid, nm in rows if normalize_name(nm) == normalize_name(name)]
        if len(matches) > 1 and not allow_ambiguous:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(matches)} chats are named {name!r}: "
                    f"{', '.join(matches)}; pass allow_ambiguous=true to send "
                    f"to {chat_id!r} anyway"
                ),
            )


async def assert_can_mutate_message(
    conn: AsyncConnection,
    chat_id: str,
    message_id: str,
    confirm_name: str,
    allow_group: bool,
    allow_ambiguous: bool,
    action: str,
) -> MessageTarget:
    """Apply recipient, ownership, state, and WhatsApp time-window guards."""
    await assert_can_send(
        conn, chat_id, confirm_name, allow_group, allow_ambiguous
    )
    cur = await conn.execute(
        "SELECT body, ts, from_me, type, revoked_at "
        "FROM messages WHERE id = %s AND chat_id = %s",
        (message_id, chat_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown message_id {message_id!r} in chat {chat_id!r}; "
                "it is not in the archive"
            ),
        )

    body, ts, from_me, message_type, revoked_at = row
    if not from_me:
        raise HTTPException(
            status_code=403,
            detail="only messages sent by the owner can be changed",
        )
    if revoked_at is not None:
        raise HTTPException(status_code=409, detail="message is already revoked")

    windows = {"edit": EDIT_WINDOW, "delete": DELETE_WINDOW}
    if action not in windows:
        raise ValueError(f"unsupported message action: {action}")
    now = datetime.now(timezone.utc)
    if now - ts > windows[action]:
        label = "15 minutes" if action == "edit" else "2 days"
        raise HTTPException(
            status_code=409,
            detail=f"WhatsApp {action} window expired ({label})",
        )
    if action == "edit" and message_type not in ("chat", "media"):
        raise HTTPException(
            status_code=409,
            detail=f"message type {message_type!r} cannot be edited",
        )
    return MessageTarget(body=body, ts=ts, message_type=message_type)


async def record_send(
    conn: AsyncConnection,
    chat_id: str,
    text: str,
    ok: bool,
    error: str | None = None,
    uncertain: bool = False,
) -> None:
    """Grava toda tentativa de envio, aceita ou recusada, e faz commit.

    sent_log não tem FK para chats de propósito: uma recusa por chat_id
    inexistente ainda precisa ficar registrada. O commit é explícito porque a
    conexão do pool não está em autocommit — sem ele, a linha some no
    rollback que a HTTPException de recusa dispara, justo no caminho que mais
    precisa ficar auditado.
    """
    await conn.execute(
        "INSERT INTO sent_log (chat_id, text, ok, error, uncertain) "
        "VALUES (%s, %s, %s, %s, %s)",
        (chat_id, text, ok, error, uncertain),
    )
    await conn.commit()


async def record_message_action(
    conn: AsyncConnection,
    action: str,
    chat_id: str,
    message_id: str,
    previous_text: str | None,
    new_text: str | None,
    ok: bool,
    error: str | None = None,
    uncertain: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO message_action_log (
          action, chat_id, message_id, previous_text, new_text,
          ok, error, uncertain
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            action,
            chat_id,
            message_id,
            previous_text,
            new_text,
            ok,
            error,
            uncertain,
        ),
    )
    await conn.commit()
