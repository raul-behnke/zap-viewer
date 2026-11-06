import hashlib
import hmac as hmac_mod
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from psycopg_pool import AsyncConnectionPool

import app.main as main_module
import app.sync as sync_module
from app.config import settings
from app.db import get_pool
from app.main import app


def _post_hook(client: TestClient, body_obj: dict[str, Any], key: str) -> httpx.Response:
    body = json.dumps(body_obj).encode()
    sig = hmac_mod.new(key.encode(), body, hashlib.sha512).hexdigest()
    return client.post(
        "/webhook/waha",
        content=body,
        headers={
            "X-Webhook-Hmac": sig,
            "Content-Type": "application/json",
        },
    )


def test_health_needs_no_key() -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_protected_route_rejects_missing_key() -> None:
    with TestClient(app) as client:
        assert client.get("/api/session/status").status_code == 401


def test_protected_route_rejects_wrong_key() -> None:
    with TestClient(app) as client:
        r = client.get(
            "/api/session/status", headers={"X-API-Key": "nope"}
        )
        assert r.status_code == 401


def test_protected_route_rejects_non_ascii_key_without_500() -> None:
    """M9: hmac.compare_digest raises TypeError on a non-ASCII str, which
    without encoding to bytes first turns a wrong key into a 500. httpx
    itself refuses non-ASCII str header values, so send raw UTF-8 bytes —
    ASGI decodes headers as latin-1, which still yields a non-ASCII str."""
    with TestClient(app) as client:
        r = client.get(
            "/api/session/status",
            headers={"X-API-Key": "café".encode()},
        )
        assert r.status_code == 401


def test_webhook_rejects_unsigned_request() -> None:
    with TestClient(app) as client:
        r = client.post("/webhook/waha", json={"event": "message.any"})
        assert r.status_code == 401


def test_webhook_rejects_bad_signature() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/webhook/waha",
            content=b'{"event":"message.any"}',
            headers={"X-Webhook-Hmac": "00", "Content-Type": "application/json"},
        )
        assert r.status_code == 401


def test_webhook_accepts_signed_request() -> None:
    with TestClient(app) as client:
        r = _post_hook(
            client,
            {"event": "session.status", "payload": {"status": "WORKING"}},
            settings.hook_hmac_key,
        )
        assert r.status_code == 200


def test_webhook_returns_200_on_broken_payload() -> None:
    with TestClient(app) as client:
        r = _post_hook(
            client,
            {"event": "message.any", "payload": {"missing": "everything"}},
            settings.hook_hmac_key,
        )
        assert r.status_code == 200


async def test_webhook_returns_200_on_malformed_json() -> None:
    body = b"{not valid json"
    sig = hmac_mod.new(
        settings.hook_hmac_key.encode(), body, hashlib.sha512
    ).hexdigest()
    with TestClient(app) as client:
        r = client.post(
            "/webhook/waha",
            content=body,
            headers={"X-Webhook-Hmac": sig, "Content-Type": "application/json"},
        )
        assert r.status_code == 200

        # event stays "" only when JSON parsing itself failed (the case
        # under test), distinguishing it from a parseable-but-broken payload.
        pool = get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT error FROM webhook_log "
                "WHERE event = '' AND error IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] is not None


def test_index_serves_html() -> None:
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "zap viewer" in r.text


def test_events_rejects_wrong_query_key() -> None:
    with TestClient(app) as client:
        r = client.get("/api/events?x_api_key=nope")
        assert r.status_code == 401


async def test_lifespan_rejects_dev_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "settings", replace(settings, api_key="dev"))
    with pytest.raises(RuntimeError):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_rejects_dev_hook_hmac_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module, "settings", replace(settings, hook_hmac_key="dev")
    )
    with pytest.raises(RuntimeError):
        async with main_module.lifespan(main_module.app):
            pass


async def test_events_accepts_correct_query_key() -> None:
    # Calling the route function directly (not through TestClient) avoids
    # opening a real SSE stream, which never closes on its own.
    before = set(main_module._subscribers)
    response = await main_module.events(x_api_key=settings.api_key)
    main_module._subscribers.difference_update(
        main_module._subscribers - before
    )
    assert isinstance(response, StreamingResponse)


def test_pair_page_serves_html() -> None:
    with TestClient(app) as client:
        r = client.get("/pair")
        assert r.status_code == 200
        assert "Conectar WhatsApp" in r.text


def test_session_stop_requires_key() -> None:
    with TestClient(app) as client:
        assert client.post("/api/session/stop").status_code == 401


async def _client() -> httpx.AsyncClient:
    # ASGITransport, no lifespan: avoids starting the real backfill_loop
    # background task against a real WAHA instance for these tests.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class _EmptyWaha:
    """Enough of the Waha surface for backfill_all to complete instantly."""

    async def chats_overview(self, limit: int, offset: int) -> list[dict]:
        return []


async def test_backfill_start_then_status_reports_fields(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "waha", _EmptyWaha())
    monkeypatch.setattr(sync_module, "_current_pass", None)
    headers = {"X-API-Key": settings.api_key}

    async with await _client() as client:
        r = await client.post("/api/backfill/start", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] in ("started", "already_running")

        r2 = await client.get("/api/backfill/status", headers=headers)
        assert r2.status_code == 200
        data = r2.json()
        # existing fields the frontend reads must still be present
        for key in ("chats", "chats_done", "messages", "oldest_cursor"):
            assert key in data
        # new fields for the owner to see pass state / staleness
        assert "running" in data
        assert "oldest_updated_at" in data


async def test_backfill_start_refuses_second_pass(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    release = asyncio.Event()

    class SlowWaha:
        async def chats_overview(self, limit: int, offset: int) -> list[dict]:
            await release.wait()
            return []

    monkeypatch.setattr(main_module, "waha", SlowWaha())
    monkeypatch.setattr(sync_module, "_current_pass", None)
    headers = {"X-API-Key": settings.api_key}

    async with await _client() as client:
        r1 = await client.post("/api/backfill/start", headers=headers)
        assert r1.json() == {"status": "started"}

        r2 = await client.post("/api/backfill/start", headers=headers)
        assert r2.json() == {"status": "already_running"}

    release.set()
    task = sync_module._current_pass
    assert task is not None
    await task
