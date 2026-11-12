import os
from collections.abc import AsyncIterator

import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql://zap:zap@localhost:55432/zap_test"
)
# app.config rejects the "dev" default outside tests (finding I7); give the
# test run real-looking secrets so lifespan-driven tests (TestClient(app, base_url="http://localhost"))
# don't trip that guard.
os.environ.setdefault("WHATSAPP_HOOK_HMAC_KEY", "test-hook-hmac-key")

from app.db import close_db, get_pool, init_db  # noqa: E402


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    await init_db(os.environ["DATABASE_URL"])
    p = get_pool()
    async with p.connection() as conn:
        await conn.execute(
            "TRUNCATE messages, sync_state, chats, webhook_log, "
            "message_action_log CASCADE"
        )
    yield p
    await _cancel_running_backfill()
    await close_db()


async def _cancel_running_backfill() -> None:
    """Encerra um passe de backfill que o teste deixou vivo.

    Sem isso a task morre junto com o event loop do teste, ainda dentro do
    `async with pool.connection()` do backfill_all. O `__aexit__` nunca roda, e
    `close_db()` não força o fecho de uma conexão que continua emprestada — ela
    sobrevive `idle in transaction`, segurando lock, e o TRUNCATE do teste
    seguinte espera por ela para sempre. Já travou a suíte duas vezes.
    """
    from app.sync import cancel_backfill_pass

    await cancel_backfill_pass()


from collections.abc import Iterator  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.db import SCHEMA_PATH  # noqa: E402


@pytest.fixture
def db() -> Iterator[psycopg.Connection]:
    """Conexão síncrona ao banco de teste, schema aplicado e tabelas limpas.

    Os testes de envio usam TestClient, que é síncrono; semear dados por aqui
    evita ter que entrar no event loop do app só para um INSERT.
    """
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    conn.execute(SCHEMA_PATH.read_text())
    conn.execute(
        "TRUNCATE messages, sync_state, chats, webhook_log, "
        "message_action_log CASCADE"
    )
    conn.execute("TRUNCATE sent_log")
    yield conn
    conn.close()
