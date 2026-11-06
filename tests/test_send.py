import base64

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import settings

HEADERS = {"X-API-Key": settings.api_key}


@pytest.fixture
def captured(monkeypatch):
    calls = {}

    async def fake_send_text(chat_id, text, reply_to):
        calls["text"] = (chat_id, text, reply_to)
        return {"id": "sent1"}

    async def fake_send_media(chat_id, filename, mimetype, data_b64, caption):
        calls["media"] = (chat_id, filename, mimetype, data_b64, caption)
        return {"id": "sent2"}

    async def fake_send_seen(chat_id, message_ids):
        calls["seen"] = (chat_id, message_ids)
        return {"result": True}

    async def fake_edit_message(chat_id, message_id, text):
        calls["edit"] = (chat_id, message_id, text)
        return {"id": message_id}

    async def fake_delete_message(chat_id, message_id):
        calls["delete"] = (chat_id, message_id)
        return {"id": message_id}

    monkeypatch.setattr(main.waha, "send_text", fake_send_text)
    monkeypatch.setattr(main.waha, "send_media", fake_send_media)
    monkeypatch.setattr(main.waha, "send_seen", fake_send_seen)
    monkeypatch.setattr(main.waha, "edit_message", fake_edit_message)
    monkeypatch.setattr(main.waha, "delete_message", fake_delete_message)
    return calls


@pytest.fixture
def seeded(db):
    db.execute(
        "INSERT INTO chats (id, name, is_group) VALUES "
        "('5511999@c.us', 'Marcos Silva', false), "
        "('120363@g.us', 'Equipe Obra', true)"
    )
    return db


@pytest.fixture
def seeded_messages(seeded):
    seeded.execute(
        """
        INSERT INTO messages (
          id, chat_id, ts, from_me, body, type, has_media, raw
        ) VALUES
          ('own-recent', '5511999@c.us', now() - interval '5 minutes',
           true, 'texto antigo', 'chat', false, '{"id":"own-recent"}'),
          ('incoming-recent', '5511999@c.us', now() - interval '5 minutes',
           false, 'texto recebido', 'chat', false, '{"id":"incoming-recent"}'),
          ('own-old-edit', '5511999@c.us', now() - interval '20 minutes',
           true, 'antigo', 'chat', false, '{"id":"own-old-edit"}'),
          ('own-old-delete', '5511999@c.us', now() - interval '3 days',
           true, 'muito antigo', 'chat', false, '{"id":"own-old-delete"}')
        """
    )
    return seeded


def test_send_text(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "5511999@c.us",
                "text": "oi",
                "confirm_name": "Marcos Silva",
            },
            headers=HEADERS,
        )
        assert r.status_code == 200
        assert captured["text"] == ("5511999@c.us", "oi", None)


def test_send_text_to_unknown_chat_never_reaches_waha(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "5599999@c.us",
                "text": "oi",
                "confirm_name": "Fulano",
            },
            headers=HEADERS,
        )
    assert r.status_code == 404
    assert "text" not in captured
    row = seeded.execute("SELECT ok, error FROM sent_log").fetchone()
    assert row[0] is False
    assert row[1]


def test_send_text_with_wrong_name_never_reaches_waha(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "5511999@c.us",
                "text": "oi",
                "confirm_name": "Marcos Souza",
            },
            headers=HEADERS,
        )
    assert r.status_code == 409
    assert "Marcos Silva" in r.json()["detail"]
    assert "text" not in captured
    row = seeded.execute("SELECT ok, error FROM sent_log").fetchone()
    assert row[0] is False
    assert row[1]


def test_send_text_to_group_needs_allow_group(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "120363@g.us",
                "text": "oi",
                "confirm_name": "Equipe Obra",
            },
            headers=HEADERS,
        )
        assert r.status_code == 409
        assert "text" not in captured

        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "120363@g.us",
                "text": "oi",
                "confirm_name": "Equipe Obra",
                "allow_group": True,
            },
            headers=HEADERS,
        )
        assert r.status_code == 200
        assert captured["text"][0] == "120363@g.us"


def test_successful_send_is_logged(captured, seeded):
    with TestClient(main.app) as client:
        client.post(
            "/api/send/text",
            json={
                "chat_id": "5511999@c.us",
                "text": "oi",
                "confirm_name": "Marcos Silva",
            },
            headers=HEADERS,
        )
    row = seeded.execute("SELECT chat_id, text, ok FROM sent_log").fetchone()
    assert row == ("5511999@c.us", "oi", True)


def test_waha_failure_is_logged_as_a_failed_attempt(monkeypatch, captured, seeded):
    async def boom(chat_id, text, reply_to):
        raise main.WahaError("down")

    monkeypatch.setattr(main.waha, "send_text", boom)
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text",
            json={
                "chat_id": "5511999@c.us",
                "text": "oi",
                "confirm_name": "Marcos Silva",
            },
            headers=HEADERS,
        )
    assert r.status_code == 503
    row = seeded.execute("SELECT ok, error, uncertain FROM sent_log").fetchone()
    assert row[0] is False
    assert row[1]
    assert row[2] is True
    assert "outcome unknown" in r.json()["detail"]


def test_edit_own_recent_message(captured, seeded_messages):
    with TestClient(main.app) as client:
        r = client.put(
            "/api/chats/5511999@c.us/messages/own-recent",
            json={"text": "texto novo", "confirm_name": "Marcos Silva"},
            headers=HEADERS,
        )
    assert r.status_code == 200
    assert captured["edit"] == (
        "5511999@c.us",
        "own-recent",
        "texto novo",
    )
    row = seeded_messages.execute(
        "SELECT action, previous_text, new_text, ok, uncertain "
        "FROM message_action_log"
    ).fetchone()
    assert row == ("edit", "texto antigo", "texto novo", True, False)


def test_edit_rejects_incoming_and_expired_messages(captured, seeded_messages):
    with TestClient(main.app) as client:
        incoming = client.put(
            "/api/chats/5511999@c.us/messages/incoming-recent",
            json={"text": "não pode", "confirm_name": "Marcos Silva"},
            headers=HEADERS,
        )
        expired = client.put(
            "/api/chats/5511999@c.us/messages/own-old-edit",
            json={"text": "tarde demais", "confirm_name": "Marcos Silva"},
            headers=HEADERS,
        )
    assert incoming.status_code == 403
    assert expired.status_code == 409
    assert "edit" not in captured


def test_delete_requires_exact_message_confirmation(captured, seeded_messages):
    with TestClient(main.app) as client:
        refused = client.request(
            "DELETE",
            "/api/chats/5511999@c.us/messages/own-recent",
            json={
                "confirm_name": "Marcos Silva",
                "confirm_message_id": "wrong-id",
            },
            headers=HEADERS,
        )
        accepted = client.request(
            "DELETE",
            "/api/chats/5511999@c.us/messages/own-recent",
            json={
                "confirm_name": "Marcos Silva",
                "confirm_message_id": "own-recent",
            },
            headers=HEADERS,
        )
    assert refused.status_code == 409
    assert accepted.status_code == 200
    assert captured["delete"] == ("5511999@c.us", "own-recent")
    rows = seeded_messages.execute(
        "SELECT ok, previous_text FROM message_action_log "
        "WHERE action = 'delete' ORDER BY id"
    ).fetchall()
    assert rows == [(False, "texto antigo"), (True, "texto antigo")]


def test_delete_rejects_expired_message(captured, seeded_messages):
    with TestClient(main.app) as client:
        r = client.request(
            "DELETE",
            "/api/chats/5511999@c.us/messages/own-old-delete",
            json={
                "confirm_name": "Marcos Silva",
                "confirm_message_id": "own-old-delete",
            },
            headers=HEADERS,
        )
    assert r.status_code == 409
    assert "delete" not in captured


def test_mutation_failure_is_marked_uncertain(
    monkeypatch, captured, seeded_messages
):
    async def boom(chat_id, message_id, text):
        raise main.WahaError("timeout")

    monkeypatch.setattr(main.waha, "edit_message", boom)
    with TestClient(main.app) as client:
        r = client.put(
            "/api/chats/5511999@c.us/messages/own-recent",
            json={"text": "talvez", "confirm_name": "Marcos Silva"},
            headers=HEADERS,
        )
    assert r.status_code == 503
    assert "outcome unknown" in r.json()["detail"]
    row = seeded_messages.execute(
        "SELECT ok, uncertain FROM message_action_log"
    ).fetchone()
    assert row == (False, True)


def test_message_action_history_can_be_filtered(captured, seeded_messages):
    with TestClient(main.app) as client:
        client.put(
            "/api/chats/5511999@c.us/messages/own-recent",
            json={"text": "texto novo", "confirm_name": "Marcos Silva"},
            headers=HEADERS,
        )
        r = client.get(
            "/api/message-actions",
            params={"message_id": "own-recent"},
            headers=HEADERS,
        )
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["action"] == "edit"


def test_send_text_requires_key(captured):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/text", json={"chat_id": "x", "text": "y"}
        )
        assert r.status_code == 401


def test_send_media_passes_base64_and_mimetype(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/media",
            data={
                "chat_id": "5511999@c.us",
                "caption": "olha",
                "confirm_name": "Marcos Silva",
            },
            files={"file": ("foto.jpg", b"jpegbytes", "image/jpeg")},
            headers=HEADERS,
        )
        assert r.status_code == 200
        chat_id, filename, mimetype, data_b64, caption = captured["media"]
        assert chat_id == "5511999@c.us"
        assert filename == "foto.jpg"
        assert mimetype == "image/jpeg"
        assert base64.b64decode(data_b64) == b"jpegbytes"
        assert caption == "olha"


def test_send_media_to_unknown_chat_never_reaches_waha(captured, seeded):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/media",
            data={"chat_id": "5599999@c.us", "confirm_name": "Fulano"},
            files={"file": ("foto.jpg", b"jpegbytes", "image/jpeg")},
            headers=HEADERS,
        )
    assert r.status_code == 404
    assert "media" not in captured
    row = seeded.execute("SELECT ok, error FROM sent_log").fetchone()
    assert row[0] is False
    assert row[1]


def test_send_media_rejects_oversized_upload(captured):
    big = b"x" * (64 * 1024 * 1024 + 1)
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/media",
            data={"chat_id": "5511999@c.us"},
            files={"file": ("big.bin", big, "application/octet-stream")},
            headers=HEADERS,
        )
        assert r.status_code == 413
        assert "media" not in captured


def test_send_seen(captured):
    with TestClient(main.app) as client:
        r = client.post(
            "/api/send/seen",
            json={"chat_id": "5511999@c.us", "message_ids": ["m1"]},
            headers=HEADERS,
        )
        assert r.status_code == 200
        assert captured["seen"] == ("5511999@c.us", ["m1"])


async def test_broadcast_reaches_subscribers():
    from asyncio import Queue

    q: Queue = Queue(maxsize=10)
    main._subscribers.add(q)
    try:
        await main.broadcast("message.any", {"id": "abc"})
        item = q.get_nowait()
        assert item == {"event": "message.any", "payload": {"id": "abc"}}
    finally:
        main._subscribers.discard(q)


async def test_broadcast_drops_full_subscriber():
    from asyncio import Queue

    q: Queue = Queue(maxsize=1)
    q.put_nowait({"filler": True})
    main._subscribers.add(q)
    try:
        await main.broadcast("message.any", {"id": "abc"})
        assert q not in main._subscribers
    finally:
        main._subscribers.discard(q)
