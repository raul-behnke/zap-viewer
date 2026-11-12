from datetime import datetime, timedelta, timezone

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

import app.main as main
from app.config import settings
from app.sync import upsert_message

HEADERS: dict[str, str] = {}

WITH_MEDIA = {
    "id": "mm1",
    "timestamp": 1767225600,
    "from": "5511999@c.us",
    "to": "5511000@c.us",
    "fromMe": False,
    "body": "foto",
    "hasMedia": True,
    "ack": 1,
    "media": {
        "url": "http://waha:3000/api/files/x.jpg",
        "mimetype": "image/jpeg",
        "filename": "x.jpg",
        "error": None,
    },
}


@pytest.fixture
async def with_media(pool: AsyncConnectionPool) -> AsyncConnectionPool:
    # pool.connection() commits on clean exit, so this row is visible to
    # the handler below, which acquires its own connection from the pool.
    async with pool.connection() as conn:
        await upsert_message(conn, WITH_MEDIA, 30)
    return pool


async def test_media_streams_bytes(
    with_media: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(url: str) -> httpx.Response:
        return httpx.Response(
            200, content=b"\xff\xd8jpegbytes",
            headers={"Content-Type": "image/jpeg"},
        )

    monkeypatch.setattr(main.waha, "fetch_media", fake_fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/mm1", headers=HEADERS)
        assert r.status_code == 200
        assert r.content == b"\xff\xd8jpegbytes"
        assert r.headers["content-type"].startswith("image/jpeg")


async def test_media_404_for_unknown_message(
    with_media: AsyncConnectionPool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/nope", headers=HEADERS)
        assert r.status_code == 404


async def test_media_410_when_expired(pool: AsyncConnectionPool) -> None:
    past = datetime.now(tz=timezone.utc) - timedelta(days=1)
    async with pool.connection() as conn:
        await upsert_message(conn, WITH_MEDIA, 30)
        await conn.execute(
            "UPDATE messages SET media_expires_at = %s WHERE id = 'mm1'",
            (past,),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/mm1", headers=HEADERS)
        assert r.status_code == 410


async def test_media_html_mime_is_not_served_as_html(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a sender-controlled media_mime of text/html must never round-trip
    as text/html — a blob URL for that response would execute on the API's
    own origin, next to the API key in localStorage."""
    hostile = {**WITH_MEDIA, "id": "hostile", "media": {**WITH_MEDIA["media"], "mimetype": "text/html"}}
    async with pool.connection() as conn:
        await upsert_message(conn, hostile, 30)

    async def fake_fetch(url: str) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<script>alert(localStorage.apikey)</script>",
            headers={"Content-Type": "text/html"},
        )

    monkeypatch.setattr(main.waha, "fetch_media", fake_fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/hostile", headers=HEADERS)
        assert r.status_code == 200
        assert "text/html" not in r.headers["content-type"]
        assert r.headers["content-type"].startswith("application/octet-stream")


async def test_media_svg_mime_is_not_served_as_svg(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 re-review: image/svg+xml passes the image/ prefix check but is a
    scriptable document — <svg><script>...</script></svg> would execute on
    the API's origin if opened as a blob URL, same as text/html."""
    hostile = {**WITH_MEDIA, "id": "svg-hostile", "media": {**WITH_MEDIA["media"], "mimetype": "image/svg+xml"}}
    async with pool.connection() as conn:
        await upsert_message(conn, hostile, 30)

    async def fake_fetch(url: str) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<svg><script>alert(localStorage.apikey)</script></svg>",
            headers={"Content-Type": "image/svg+xml"},
        )

    monkeypatch.setattr(main.waha, "fetch_media", fake_fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/svg-hostile", headers=HEADERS)
        assert r.status_code == 200
        assert "image/svg+xml" not in r.headers["content-type"]
        assert r.headers["content-type"].startswith("application/octet-stream")
        assert r.headers["x-content-type-options"] == "nosniff"


async def test_media_content_disposition_handles_non_latin1_filename(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I5: a Portuguese filename with an accent must not 500."""
    accented = {
        **WITH_MEDIA,
        "id": "accented",
        "media": {**WITH_MEDIA["media"], "filename": "relatório \"final\".pdf", "mimetype": "application/pdf"},
    }
    async with pool.connection() as conn:
        await upsert_message(conn, accented, 30)

    async def fake_fetch(url: str) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4")

    monkeypatch.setattr(main.waha, "fetch_media", fake_fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/accented", headers=HEADERS)
        assert r.status_code == 200
        disposition = r.headers["content-disposition"]
        assert "relat" in disposition
        assert '"' not in disposition.split("filename*=UTF-8''", 1)[1]


async def test_media_410_when_waha_says_gone(
    with_media: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(url: str) -> httpx.Response:
        return httpx.Response(404, content=b"")

    monkeypatch.setattr(main.waha, "fetch_media", fake_fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://localhost"
    ) as client:
        r = await client.get("/api/media/mm1", headers=HEADERS)
        assert r.status_code == 410


def test_resolve_media_target_accepts_s3_paths() -> None:
    """WAHA emits /api/s3/<bucket>/... once media storage is R2."""
    from app.waha import Waha

    w = Waha("http://waha:3000", "k", "default")
    target = w._resolve_media_target(
        "http://localhost:3000/api/s3/zap-media/default/ABC.png"
    )
    assert target == "http://waha:3000/api/s3/zap-media/default/ABC.png"


def test_resolve_media_target_neutralizes_a_foreign_host() -> None:
    """A stored URL naming another host is not contacted: the host is discarded
    and the path is resolved against our own WAHA, so the X-Api-Key cannot
    egress. Refusal is reserved for paths that are not media paths."""
    from app.waha import Waha, WahaError

    w = Waha("http://waha:3000", "k", "default")
    assert w._resolve_media_target(
        "http://evil.example/api/s3/zap-media/default/ABC.png"
    ) == "http://waha:3000/api/s3/zap-media/default/ABC.png"

    for bad in (
        "http://localhost:3000/etc/passwd",
        "http://localhost:3000/api/sessions/default",
    ):
        try:
            w._resolve_media_target(bad)
        except WahaError:
            continue
        raise AssertionError(f"should have refused {bad}")
