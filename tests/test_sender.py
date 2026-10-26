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
        chat_id=777, text="ответ", business_connection_id="c1", parse_mode=None
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


@pytest.mark.asyncio
async def test_forbidden_returns_error_instead_of_raising(ready_conn):
    from aiogram.exceptions import TelegramForbiddenError

    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(method=AsyncMock(), message="bot was blocked")
    res = await send_business_reply(bot, ready_conn, 777, "x")
    assert res["ok"] is False and "blocked" in res["error"]


@pytest.mark.asyncio
async def test_second_retry_after_returns_error(ready_conn, monkeypatch):
    import tg_business_bridge.sender as sender_mod

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(sender_mod.asyncio, "sleep", fake_sleep)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramRetryAfter(method=AsyncMock(), message="flood", retry_after=3),
        TelegramRetryAfter(method=AsyncMock(), message="flood", retry_after=30),
    ]
    res = await send_business_reply(bot, ready_conn, 777, "x")
    assert res["ok"] is False  # повторный flood не должен пробрасываться исключением


@pytest.mark.asyncio
async def test_send_html_passes_parse_mode(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.return_value = _sent_message(text="смотри тут")
    res = await send_business_reply(
        bot, ready_conn, 777, '<a href="https://ya.ru">смотри тут</a>', "HTML"
    )
    assert res == {"ok": True, "error": None}
    call = bot.send_message.await_args
    assert call.kwargs["parse_mode"] == "HTML"
    assert call.kwargs["text"] == '<a href="https://ya.ru">смотри тут</a>'


def _doc_message(message_id=99, **over) -> Message:
    return _sent_message(
        message_id=message_id, text=None,
        document={"file_id": f"D{message_id}", "file_unique_id": f"U{message_id}"}, **over,
    )


@pytest.mark.asyncio
async def test_single_attachment_with_caption(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_document.return_value = _doc_message(caption="вот файл")
    res = await send_business_reply(
        bot, ready_conn, 777, "вот файл", None, [{"kind": "document", "file": "FILE1"}]
    )
    assert res == {"ok": True, "error": None}
    bot.send_document.assert_awaited_once_with(
        chat_id=777, business_connection_id="c1", document="FILE1",
        caption="вот файл", parse_mode=None,
    )
    bot.send_message.assert_not_awaited()
    stored = ready_conn.execute("SELECT media_type FROM messages WHERE message_id=99").fetchone()
    assert stored["media_type"] == "document"


@pytest.mark.asyncio
async def test_single_attachment_html_caption(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_photo.return_value = _sent_message(
        text=None, photo=[{"file_id": "P", "file_unique_id": "UP", "width": 1, "height": 1}]
    )
    await send_business_reply(
        bot, ready_conn, 777, "<b>смотри</b>", "HTML", [{"kind": "photo", "file": "PH"}]
    )
    kw = bot.send_photo.await_args.kwargs
    assert kw["photo"] == "PH" and kw["caption"] == "<b>смотри</b>" and kw["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_empty_text_sends_media_only(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_document.return_value = _doc_message()
    await send_business_reply(bot, ready_conn, 777, "", None, [{"kind": "document", "file": "F"}])
    kw = bot.send_document.await_args.kwargs
    assert "caption" not in kw
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_caption_sent_as_text_first(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    order = []

    async def send_text(**kw):
        order.append("text")
        return _sent_message(message_id=98, text=kw["text"])

    async def send_doc(**kw):
        order.append("doc")
        return _doc_message()

    bot.send_message.side_effect = send_text
    bot.send_document.side_effect = send_doc
    long_text = "я" * 1025
    res = await send_business_reply(
        bot, ready_conn, 777, long_text, None, [{"kind": "document", "file": "F"}]
    )
    assert res["ok"] is True
    assert order == ["text", "doc"]
    assert bot.send_message.await_args.kwargs["text"] == long_text
    assert "caption" not in bot.send_document.await_args.kwargs


@pytest.mark.asyncio
async def test_caption_limit_counts_visible_html_text(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_document.return_value = _doc_message()
    # видимых символов 1000, с тегами и длинным href — сильно больше 1024
    text = '<a href="https://example.com/' + "x" * 200 + '">' + "я" * 1000 + "</a>"
    await send_business_reply(
        bot, ready_conn, 777, text, "HTML", [{"kind": "document", "file": "F"}]
    )
    assert bot.send_document.await_args.kwargs["caption"] == text
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exactly_1024_visible_chars_is_caption(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_document.return_value = _doc_message()
    await send_business_reply(
        bot, ready_conn, 777, "я" * 1024, None, [{"kind": "document", "file": "F"}]
    )
    assert bot.send_document.await_args.kwargs["caption"] == "я" * 1024
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_caption_on_first_item_only(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_media_group.return_value = [_doc_message(99), _doc_message(100)]
    res = await send_business_reply(
        bot, ready_conn, 777, "подпись", None,
        [{"kind": "photo", "file": "A"}, {"kind": "video", "file": "B"}],
    )
    assert res["ok"] is True
    kw = bot.send_media_group.await_args.kwargs
    assert kw["chat_id"] == 777 and kw["business_connection_id"] == "c1"
    first, second = kw["media"]
    assert type(first).__name__ == "InputMediaPhoto" and first.media == "A"
    assert first.caption == "подпись"
    assert type(second).__name__ == "InputMediaVideo" and second.media == "B"
    assert second.caption is None
    bot.send_message.assert_not_awaited()
    ids = {r["message_id"] for r in ready_conn.execute("SELECT message_id FROM messages")}
    assert {99, 100} <= ids  # оба сообщения альбома сохранены


@pytest.mark.asyncio
async def test_documents_album_with_long_text(ready_conn):
    bot = AsyncMock()
    bot.id = 99
    bot.send_message.return_value = _sent_message(message_id=98)
    bot.send_media_group.return_value = [_doc_message(99), _doc_message(100)]
    await send_business_reply(
        bot, ready_conn, 777, "я" * 2000, None,
        [{"kind": "document", "file": "A"}, {"kind": "document", "file": "B"}],
    )
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "я" * 2000
    media = bot.send_media_group.await_args.kwargs["media"]
    assert all(type(m).__name__ == "InputMediaDocument" for m in media)
    assert all(m.caption is None for m in media)


@pytest.mark.asyncio
async def test_attachment_error_returns_error(ready_conn):
    bot = AsyncMock()
    bot.send_document.side_effect = TelegramBadRequest(method=AsyncMock(), message="wrong file")
    res = await send_business_reply(
        bot, ready_conn, 777, "x", None, [{"kind": "document", "file": "F"}]
    )
    assert res["ok"] is False and "wrong file" in res["error"]
