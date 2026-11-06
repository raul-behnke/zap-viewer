from datetime import datetime, timezone

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from app.mediastat import (
    Result,
    fetch_rows,
    mime_family,
    pick_evenly_strided,
    probe,
    report,
)
from app.sync import upsert_message
from app.waha import Waha


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("image/jpeg", "image"),
        ("video/mp4", "video"),
        ("audio/ogg; codecs=opus", "audio"),
        ("application/pdf", "document"),
        (None, "document"),
    ],
)
def test_mime_family(mime: str | None, expected: str) -> None:
    assert mime_family(mime) == expected


def test_pick_evenly_strided_returns_everything_when_fewer_rows_than_n() -> None:
    rows = [(i,) for i in range(5)]
    assert pick_evenly_strided(rows, 200) == rows


def test_pick_evenly_strided_spans_the_full_range() -> None:
    rows = [(i,) for i in range(1000)]
    picked = pick_evenly_strided(rows, 10)
    assert len(picked) == 10
    values = [p[0] for p in picked]
    # spread across the whole range, not clustered at one end
    assert values[0] < 100
    assert values[-1] >= 900
    assert values == sorted(values)


def test_report_zero_successes_does_not_divide_by_zero() -> None:
    result = Result()
    result.failures["http 503"] = 3
    out = report(
        result,
        sample_target=3,
        total_media_rows=1496,
        oldest_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        newest_ts=datetime(2026, 9, 1, tzinfo=timezone.utc),
        cap_bytes=9 * 1024**3,
    )
    assert "0 of 3 samples succeeded" in out
    assert "cannot extrapolate" in out


async def test_probe_falls_back_head_to_range_to_full_get() -> None:
    """One sample answers HEAD with a length, one only answers a ranged GET
    with Content-Range, one gives neither and needs a full body GET, and one
    is already gone -- all four must be handled without raising."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/files/head.jpg":
            if request.method == "HEAD":
                return httpx.Response(200, headers={"Content-Length": "1000"})
            return httpx.Response(200, content=b"x" * 1000)
        if request.url.path == "/api/files/range.jpg":
            if request.method == "HEAD":
                return httpx.Response(200)  # no content-length
            return httpx.Response(
                206, headers={"Content-Range": "bytes 0-0/2000"}, content=b"x"
            )
        if request.url.path == "/api/files/fullget.jpg":
            if request.method == "HEAD":
                return httpx.Response(200)
            return httpx.Response(200, content=b"y" * 3000)
        if request.url.path == "/api/files/gone.jpg":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request}")

    waha = Waha("http://waha.local", "key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local", transport=httpx.MockTransport(handler)
    )

    from app.mediastat import Sample

    samples = [
        Sample("m1", None, "image/jpeg", "http://localhost:3000/api/files/head.jpg"),
        Sample("m2", None, "image/jpeg", "http://localhost:3000/api/files/range.jpg"),
        Sample("m3", None, "image/jpeg", "http://localhost:3000/api/files/fullget.jpg"),
        Sample("m4", None, "image/jpeg", "http://localhost:3000/api/files/gone.jpg"),
    ]
    result = Result()
    try:
        await probe(waha, samples, result)
    finally:
        await waha.aclose()

    assert sorted(result.ok_sizes["image"]) == [1000, 2000, 3000]
    assert result.failures == {"gone (404/410)": 1}


async def test_fetch_rows_reads_media_messages_from_db(
    pool: AsyncConnectionPool,
) -> None:
    msg = {
        "id": "mm1",
        "timestamp": 1767225600,
        "from": "5511999@c.us",
        "to": "5511000@c.us",
        "fromMe": False,
        "body": "foto",
        "hasMedia": True,
        "media": {
            "url": "http://waha:3000/api/files/x.jpg",
            "mimetype": "image/jpeg",
            "filename": "x.jpg",
        },
    }
    async with pool.connection() as conn:
        await upsert_message(conn, msg, 30)
        rows = await fetch_rows(conn)
    assert len(rows) == 1
    assert rows[0][0] == "mm1"
    assert rows[0][3] == "http://waha:3000/api/files/x.jpg"
