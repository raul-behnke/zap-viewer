import asyncio
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx


def pick_send_endpoint(mimetype: str | None) -> str:
    mt = (mimetype or "").lower()
    if mt.startswith("image/"):
        return "sendImage"
    if mt.startswith("video/"):
        return "sendVideo"
    if mt.startswith("audio/"):
        return "sendVoice"
    return "sendFile"


class WahaError(RuntimeError):
    """WAHA returned an error or could not be reached."""


class Waha:
    def __init__(self, base_url: str, api_key: str, session: str) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"X-Api-Key": api_key},
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    @property
    def session(self) -> str:
        return self._session

    async def _get(self, path: str, **params: Any) -> httpx.Response:
        clean = {k: v for k, v in params.items() if v is not None}
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.get(path, params=clean)
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as exc:
                raise WahaError(f"GET {path} failed: {exc}") from exc
            except httpx.TransportError as exc:
                last = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise WahaError(f"GET {path} failed: {last}")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(path, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise WahaError(f"POST {path} failed: {exc}") from exc
        return self._json_response(r)

    @staticmethod
    def _json_response(r: httpx.Response) -> dict[str, Any]:
        """Accept a successful WAHA response even when its body is empty.

        A 2xx response is the delivery signal. Treating a non-JSON success as
        a failure encourages a dangerous retry after the action already ran.
        """
        if not r.content:
            return {}
        try:
            data = r.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"result": data}

    async def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.put(path, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise WahaError(f"PUT {path} failed: {exc}") from exc
        return self._json_response(r)

    async def _delete(self, path: str) -> dict[str, Any]:
        try:
            r = await self._client.delete(path)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise WahaError(f"DELETE {path} failed: {exc}") from exc
        return self._json_response(r)

    async def status(self) -> dict[str, Any]:
        r = await self._get(f"/api/sessions/{self._session}")
        return r.json()

    async def start(self) -> dict[str, Any]:
        return await self._post(
            "/api/sessions/start", {"name": self._session}
        )

    async def stop(self) -> dict[str, Any]:
        return await self._post("/api/sessions/stop", {"name": self._session})

    async def qr(self) -> bytes:
        r = await self._get(f"/api/{self._session}/auth/qr", format="image")
        return r.content

    async def chats_overview(self, limit: int, offset: int) -> list[dict]:
        r = await self._get(
            f"/api/{self._session}/chats/overview", limit=limit, offset=offset
        )
        return r.json()

    async def chat_messages(
        self, chat_id: str, limit: int, before_ts: int | None
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "downloadMedia": "true"}
        if before_ts is not None:
            params["filter.timestamp.lte"] = before_ts
        r = await self._get(
            f"/api/{self._session}/chats/{chat_id}/messages", **params
        )
        return r.json()

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session": self._session,
            "chatId": chat_id,
            "text": text,
        }
        if reply_to:
            body["reply_to"] = reply_to
        return await self._post("/api/sendText", body)

    async def send_media(
        self,
        chat_id: str,
        filename: str,
        mimetype: str,
        data_b64: str,
        caption: str | None,
    ) -> dict[str, Any]:
        endpoint = pick_send_endpoint(mimetype)
        body: dict[str, Any] = {
            "session": self._session,
            "chatId": chat_id,
            "file": {
                "mimetype": mimetype,
                "filename": filename,
                "data": data_b64,
            },
        }
        if endpoint == "sendVoice":
            body["convert"] = True
        elif caption:
            body["caption"] = caption
        return await self._post(f"/api/{endpoint}", body)

    async def send_seen(
        self, chat_id: str, message_ids: list[str] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session": self._session,
            "chatId": chat_id,
        }
        if message_ids:
            body["messageIds"] = message_ids
        return await self._post("/api/sendSeen", body)

    def _message_path(self, chat_id: str, message_id: str) -> str:
        chat = quote(chat_id, safe="")
        message = quote(message_id, safe="")
        session = quote(self._session, safe="")
        return f"/api/{session}/chats/{chat}/messages/{message}"

    async def edit_message(
        self, chat_id: str, message_id: str, text: str
    ) -> dict[str, Any]:
        return await self._put(
            self._message_path(chat_id, message_id),
            {"text": text, "linkPreview": True},
        )

    async def delete_message(
        self, chat_id: str, message_id: str
    ) -> dict[str, Any]:
        return await self._delete(self._message_path(chat_id, message_id))

    def _resolve_media_target(self, url: str) -> str:
        # media_url comes from messages.media_url, which WAHA itself wrote —
        # but using WAHA's own idea of its hostname (e.g.
        # http://localhost:3000/...), which from inside our container
        # resolves to us, not WAHA. Treat the stored value as a *path* on
        # our configured WAHA base rather than an absolute address: take
        # its path+query, discard whatever host it names, and resolve that
        # against self._base. Then still pin the result to our own host so
        # the X-Api-Key header can never egress to a third-party host.
        # WAHA serves media under a different prefix per storage backend:
        # /api/files/... with local storage, /api/s3/<bucket>/... with S3/R2.
        parsed = urlsplit(url)
        if not parsed.path.startswith(("/api/files/", "/api/s3/")):
            raise WahaError(f"refusing to fetch media from unexpected path: {url}")
        path_and_query = urlunsplit(("", "", parsed.path, parsed.query, ""))
        target = urljoin(self._base + "/", path_and_query.lstrip("/"))
        if urlsplit(target).netloc != urlsplit(self._base).netloc:
            raise WahaError(f"refusing to fetch media from foreign host: {target}")
        return target

    async def fetch_media(self, url: str) -> httpx.Response:
        return await self._client.get(self._resolve_media_target(url))

    async def media_size(self, url: str) -> int:
        """Byte size of stored media, without downloading it fully.

        Tries HEAD first (cheapest), falls back to a 1-byte ranged GET when
        the server doesn't answer HEAD with a length, and only falls back to
        a full GET when neither gives one.
        """
        target = self._resolve_media_target(url)

        r = await self._client.head(target)
        r.raise_for_status()
        cl = r.headers.get("content-length")
        if cl is not None:
            return int(cl)

        r = await self._client.get(target, headers={"Range": "bytes=0-0"})
        r.raise_for_status()
        content_range = r.headers.get("content-range")  # "bytes 0-0/12345"
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                return int(total)

        r = await self._client.get(target)
        r.raise_for_status()
        return len(r.content)

    async def aclose(self) -> None:
        await self._client.aclose()
