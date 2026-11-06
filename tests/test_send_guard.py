import os

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.db import close_db, get_pool, init_db
from app.send_guard import assert_can_send, normalize_name, record_send


# pytest_asyncio.fixture, não pytest.fixture: é o padrão que tests/conftest.py
# já usa para fixture async, e no modo auto uma fixture async declarada com
# pytest.fixture chega no teste como generator, não como o pool.
@pytest_asyncio.fixture
async def pool_with_chats(db):
    """Pool async do app, com dois chats semeados pela conexão síncrona."""
    db.execute(
        "INSERT INTO chats (id, name, is_group) VALUES "
        "('5511111@c.us', 'Marcos Silva', false), "
        "('120363@g.us', 'Equipe Obra', true), "
        "('5522222@c.us', NULL, false), "
        "('5533333@c.us', 'João Perón', false)"
    )
    await init_db(os.environ["DATABASE_URL"])
    yield get_pool()
    await close_db()


async def test_unknown_chat_is_refused(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "5599999@c.us", "Quem Quer Que Seja", False)
    assert exc.value.status_code == 404


async def test_name_mismatch_is_refused_and_reveals_the_real_name(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "5511111@c.us", "Marcos Souza", False)
    assert exc.value.status_code == 409
    assert "Marcos Silva" in exc.value.detail


async def test_matching_name_passes_despite_accent_and_case(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        await assert_can_send(conn, "5533333@c.us", "joao peron", False)


async def test_group_needs_explicit_permission(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "120363@g.us", "Equipe Obra", False)
    assert exc.value.status_code == 409
    async with pool_with_chats.connection() as conn:
        await assert_can_send(conn, "120363@g.us", "Equipe Obra", True)


async def test_nameless_chat_is_confirmed_by_its_own_id(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        await assert_can_send(conn, "5522222@c.us", "5522222@c.us", False)


async def test_blank_confirm_name_is_refused_for_the_nameless_chat(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "5522222@c.us", "", False)
    assert exc.value.status_code == 409
    async with pool_with_chats.connection() as conn:
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "5522222@c.us", "   ", False)
    assert exc.value.status_code == 409


async def test_ambiguous_name_is_refused_by_default(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        await conn.execute(
            "INSERT INTO chats (id, name, is_group) VALUES "
            "('5511112@c.us', 'Marcos Silva', false)"
        )
        await conn.commit()
        with pytest.raises(HTTPException) as exc:
            await assert_can_send(conn, "5511111@c.us", "Marcos Silva", False)
    assert exc.value.status_code == 409
    assert "5511111@c.us" in exc.value.detail
    assert "5511112@c.us" in exc.value.detail


async def test_ambiguous_name_passes_with_explicit_permission(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        await conn.execute(
            "INSERT INTO chats (id, name, is_group) VALUES "
            "('5511112@c.us', 'Marcos Silva', false)"
        )
        await conn.commit()
        await assert_can_send(
            conn, "5511111@c.us", "Marcos Silva", False, allow_ambiguous=True
        )


async def test_nameless_chat_is_never_treated_as_ambiguous(pool_with_chats):
    # Chat sem nome usa o próprio id como confirmação, e id é único — mesmo
    # com outros chats sem nome no banco, a checagem de ambiguidade não entra
    # em jogo para eles.
    async with pool_with_chats.connection() as conn:
        await conn.execute(
            "INSERT INTO chats (id, name, is_group) VALUES "
            "('5522223@c.us', NULL, false)"
        )
        await conn.commit()
        await assert_can_send(conn, "5522222@c.us", "5522222@c.us", False)


async def test_record_send_keeps_the_error(pool_with_chats):
    async with pool_with_chats.connection() as conn:
        await record_send(conn, "5511111@c.us", "oi", False, "waha unavailable")
        cur = await conn.execute("SELECT chat_id, ok, error FROM sent_log")
        assert await cur.fetchone() == ("5511111@c.us", False, "waha unavailable")


async def test_record_send_survives_a_rollback_on_a_separate_connection(
    pool_with_chats,
):
    # O caso real: o handler chama record_send e depois levanta HTTPException
    # dentro do mesmo `async with pool.connection()`. Psycopg dá rollback na
    # saída por exceção — se record_send não desse commit, a linha sumiria
    # junto. A prova só vale lida por OUTRA conexão, senão ainda estaríamos
    # dentro da transação que gravou a linha.
    try:
        async with pool_with_chats.connection() as conn:
            await record_send(conn, "5511111@c.us", "oi", False, "boom")
            raise HTTPException(status_code=409, detail="simulated refusal")
    except HTTPException:
        pass

    async with pool_with_chats.connection() as other_conn:
        cur = await other_conn.execute("SELECT count(*) FROM sent_log")
        row = await cur.fetchone()
        assert row[0] == 1


def test_db_fixture_gives_a_clean_sent_log(db):
    db.execute(
        "INSERT INTO sent_log (chat_id, text, ok) VALUES (%s, %s, %s)",
        ("5511999@c.us", "oi", True),
    )
    row = db.execute("SELECT count(*) FROM sent_log").fetchone()
    assert row[0] == 1


def test_normalize_name_ignores_case_and_accent():
    assert normalize_name("João") == normalize_name("joao")


def test_normalize_name_collapses_whitespace():
    assert normalize_name("  Equipe   Obra ") == normalize_name("Equipe Obra")


def test_normalize_name_handles_none():
    assert normalize_name(None) == ""


def test_normalize_name_keeps_distinct_names_distinct():
    assert normalize_name("Marcos Silva") != normalize_name("Marcos Souza")
