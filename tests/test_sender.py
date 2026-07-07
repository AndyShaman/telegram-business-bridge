from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

from tg_business_bridge import db
from tg_business_bridge.sender import send_business_reply
from test_db import _msg


def _sent_message(**over) -> Message:
    d = {
        "message_id": 99,
        "date": 1751800000,
        "chat": {"id": 777, "type": "private", "first_name": "Alice"},
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "business_connection_id": "c1",
        "text": "ответ",
    }
    d.update(over)
    return Message.model_validate(d)


@pytest.fixture()
def ready_conn(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": true, "can_read_messages": true}', True)
    db.insert_message(conn, _msg(message_id=10, direction="in"))
    return conn


@pytest.mark.asyncio
async def test_send_happy_path(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.return_value = _sent_message()
    res = await send_business_reply(bot, ready_conn, 777, "ответ")
    assert res == {"ok": True, "error": None}
    bot.send_message.assert_awaited_once_with(
        chat_id=777, text="ответ", business_connection_id="c1"
    )
    bot.read_business_message.assert_awaited_once_with(
        business_connection_id="c1", chat_id=777, message_id=10
    )


@pytest.mark.asyncio
async def test_send_happy_path_stores_sent_message(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.return_value = _sent_message()
    res = await send_business_reply(bot, ready_conn, 777, "ответ")
    assert res == {"ok": True, "error": None}
    row = ready_conn.execute(
        "SELECT chat_id, text, direction FROM messages WHERE message_id=99"
    ).fetchone()
    assert row is not None
    assert row["chat_id"] == 777
    assert row["text"] == "ответ"
    assert row["direction"] == "out"


@pytest.mark.asyncio
async def test_no_connection(conn):
    res = await send_business_reply(AsyncMock(), conn, 777, "x")
    assert res["ok"] is False and "connection" in res["error"]


@pytest.mark.asyncio
async def test_no_can_reply(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": false}', True)
    res = await send_business_reply(AsyncMock(), conn, 777, "x")
    assert res["ok"] is False and "can_reply" in res["error"]


@pytest.mark.asyncio
async def test_retry_after_then_success(ready_conn, monkeypatch):
    import tg_business_bridge.sender as sender_mod

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(sender_mod.asyncio, "sleep", fake_sleep)
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.side_effect = [
        TelegramRetryAfter(method=AsyncMock(), message="flood", retry_after=3),
        _sent_message(),
    ]
    res = await send_business_reply(bot, ready_conn, 777, "x")
    assert res["ok"] is True and sleeps == [3]
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_bad_request_returns_error(ready_conn):
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(method=AsyncMock(), message="BUSINESS_PEER_INVALID")
    res = await send_business_reply(bot, ready_conn, 777, "x")
    assert res["ok"] is False and "BUSINESS_PEER_INVALID" in res["error"]
