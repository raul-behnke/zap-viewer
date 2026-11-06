import httpx
import pytest

from app.waha import Waha, WahaError, pick_send_endpoint


@pytest.mark.parametrize(
    "mimetype,expected",
    [
        ("image/jpeg", "sendImage"),
        ("image/png", "sendImage"),
        ("video/mp4", "sendVideo"),
        ("audio/ogg", "sendVoice"),
        ("audio/mpeg", "sendVoice"),
        ("application/pdf", "sendFile"),
        ("text/plain", "sendFile"),
        (None, "sendFile"),
        ("", "sendFile"),
    ],
)
def test_pick_send_endpoint(mimetype: str | None, expected: str) -> None:
    assert pick_send_endpoint(mimetype) == expected


async def test_get_does_not_retry_http_status_error() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": "not found"})

    waha = Waha("http://waha.local", "key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WahaError):
            await waha.status()
    finally:
        await waha.aclose()

    assert attempts == 1


async def test_edit_message_uses_encoded_gows_route() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.raw_path.decode()
        captured["body"] = request.read()
        return httpx.Response(200, json={"id": "edited"})

    waha = Waha("http://waha.local", "key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local", transport=httpx.MockTransport(handler)
    )
    try:
        result = await waha.edit_message(
            "5511999@c.us", "true_5511999@c.us_ABC", "texto novo"
        )
    finally:
        await waha.aclose()

    assert captured["method"] == "PUT"
    assert captured["path"] == (
        "/api/default/chats/5511999%40c.us/messages/"
        "true_5511999%40c.us_ABC"
    )
    assert b'"text":"texto novo"' in captured["body"]
    assert result == {"id": "edited"}


async def test_delete_message_uses_encoded_gows_route() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.raw_path.decode()
        return httpx.Response(200)

    waha = Waha("http://waha.local", "key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local", transport=httpx.MockTransport(handler)
    )
    try:
        result = await waha.delete_message(
            "5511999@c.us", "true_5511999@c.us_ABC"
        )
    finally:
        await waha.aclose()

    assert captured == {
        "method": "DELETE",
        "path": (
            "/api/default/chats/5511999%40c.us/messages/"
            "true_5511999%40c.us_ABC"
        ),
    }
    assert result == {}


async def test_success_with_non_json_body_is_not_treated_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")

    waha = Waha("http://waha.local", "key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local", transport=httpx.MockTransport(handler)
    )
    try:
        result = await waha.send_text("5511999@c.us", "oi", None)
    finally:
        await waha.aclose()
    assert result == {}


async def test_fetch_media_refuses_foreign_host() -> None:
    """M14: media_url is written by WAHA itself today, but fetch_media must
    not blindly forward the X-Api-Key header to whatever host an absolute
    media_url happens to point at."""
    waha = Waha("http://waha.local", "secret-key", "default")
    try:
        with pytest.raises(WahaError):
            await waha.fetch_media("http://evil.example/steal")
    finally:
        await waha.aclose()


async def test_fetch_media_allows_same_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "secret-key"
        return httpx.Response(200, content=b"ok")

    waha = Waha("http://waha.local", "secret-key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local",
        headers={"X-Api-Key": "secret-key"},
        transport=httpx.MockTransport(handler),
    )
    try:
        r = await waha.fetch_media("http://waha.local/api/files/x.jpg")
        assert r.status_code == 200
    finally:
        await waha.aclose()


async def test_fetch_media_normalizes_wahas_own_hostname() -> None:
    """M14 fix: WAHA writes media_url using its own idea of its hostname
    (e.g. http://localhost:3000/...), which from inside the API container
    resolves to the API itself, not WAHA. The path must still be resolved
    against our configured WAHA base and fetched successfully."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "waha.local"
        assert request.headers["X-Api-Key"] == "secret-key"
        return httpx.Response(200, content=b"jpegbytes")

    waha = Waha("http://waha.local", "secret-key", "default")
    waha._client = httpx.AsyncClient(
        base_url="http://waha.local",
        headers={"X-Api-Key": "secret-key"},
        transport=httpx.MockTransport(handler),
    )
    try:
        r = await waha.fetch_media("http://localhost:3000/api/files/default/x.jpg")
        assert r.status_code == 200
        assert r.content == b"jpegbytes"
    finally:
        await waha.aclose()


async def test_fetch_media_refuses_path_that_is_not_a_media_path() -> None:
    """Normalizing the host must not turn into an open proxy: only paths
    that look like WAHA's media path are resolved, everything else is
    refused even though the host gets discarded anyway."""
    waha = Waha("http://waha.local", "secret-key", "default")
    try:
        with pytest.raises(WahaError):
            await waha.fetch_media("http://evil.example/x")
        with pytest.raises(WahaError):
            await waha.fetch_media("http://waha.local/x")
    finally:
        await waha.aclose()
