from unittest.mock import AsyncMock

import pytest

from tg_business_bridge import db
from tg_business_bridge.daemon.draft_handlers import on_draft_callback, process_new_drafts
from test_db import _msg
from test_business_handlers import settings  # noqa: F401 - фикстура


@pytest.fixture()
def ready_conn(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": true}', True)
    db.insert_message(conn, _msg(message_id=10, direction="in"))
    return conn


@pytest.mark.asyncio
async def test_pending_draft_sends_card(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "черновик ответа", "pending")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 555
    await process_new_drafts(bot, ready_conn, settings)
    draft = db.get_draft(ready_conn, did)
    assert draft["status"] == "awaiting"
    assert draft["card_message_id"] == 555
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 42  # владельцу
    assert "черновик ответа" in call.kwargs["text"]
    kb = call.kwargs["reply_markup"].inline_keyboard[0]
    assert kb[0].callback_data == f"draft:{did}:approve"
    assert kb[1].callback_data == f"draft:{did}:reject"


@pytest.mark.asyncio
async def test_approved_draft_is_sent(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "ответ", "approved")
    bot = AsyncMock()
    await process_new_drafts(bot, ready_conn, settings)
    assert db.get_draft(ready_conn, did)["status"] == "sent"
    assert bot.send_message.await_args.kwargs["business_connection_id"] == "c1"


@pytest.mark.asyncio
async def test_failed_send_marks_failed(ready_conn, settings):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    did = db.create_draft(ready_conn, 777, "ответ", "approved")
    bot = AsyncMock()

    # Mock only the first send_message call to fail (from send_business_reply);
    # separate error notification is dropped, and card edit is skipped here
    # because this draft has no card_message_id.
    call_count = 0

    async def selective_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TelegramBadRequest(method=AsyncMock(), message="window expired")

    bot.send_message.side_effect = selective_side_effect
    await process_new_drafts(bot, ready_conn, settings)
    row = db.get_draft(ready_conn, did)
    assert row["status"] == "failed" and "window expired" in row["error"]


@pytest.mark.asyncio
async def test_successful_send_edits_card(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "ответ", "approved")
    db.set_draft_card(ready_conn, did, 222)
    bot = AsyncMock()
    await process_new_drafts(bot, ready_conn, settings)
    assert db.get_draft(ready_conn, did)["status"] == "sent"
    edit_call = bot.edit_message_text.await_args
    assert edit_call.kwargs["chat_id"] == 42
    assert edit_call.kwargs["message_id"] == 222
    assert "✅ Отправлено" in edit_call.kwargs["text"]


@pytest.mark.asyncio
async def test_failed_send_edits_card_no_extra_message(ready_conn, settings):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    did = db.create_draft(ready_conn, 777, "ответ", "approved")
    db.set_draft_card(ready_conn, did, 333)
    bot = AsyncMock()

    async def side_effect(*args, **kwargs):
        raise TelegramBadRequest(method=AsyncMock(), message="window expired")

    bot.send_message.side_effect = side_effect
    await process_new_drafts(bot, ready_conn, settings)
    row = db.get_draft(ready_conn, did)
    assert row["status"] == "failed"
    bot.send_message.assert_awaited_once()  # только попытка отправки, без отдельного уведомления
    edit_call = bot.edit_message_text.await_args
    assert edit_call.kwargs["chat_id"] == 42
    assert edit_call.kwargs["message_id"] == 333
    assert "⚠️ Не удалось отправить" in edit_call.kwargs["text"]


@pytest.mark.asyncio
async def test_claim_failure_skips_send(ready_conn, settings, monkeypatch):  # noqa: F811
    # Симулирует гонку: другой процесс/итерация уже забрал(а) черновик (claim провален).
    did = db.create_draft(ready_conn, 777, "ответ", "approved")
    monkeypatch.setattr(db, "claim_draft", lambda conn, draft_id: False)
    bot = AsyncMock()
    await process_new_drafts(bot, ready_conn, settings)
    bot.send_message.assert_not_called()
    assert db.get_draft(ready_conn, did)["status"] == "approved"


@pytest.mark.asyncio
async def test_callback_approve_and_reject(ready_conn, settings):  # noqa: F811
    d1 = db.create_draft(ready_conn, 777, "a", "awaiting")
    d2 = db.create_draft(ready_conn, 777, "b", "awaiting")
    bot = AsyncMock()

    def _cb(data):
        cb = AsyncMock()
        cb.data = data
        return cb

    await on_draft_callback(_cb(f"draft:{d1}:approve"), conn=ready_conn, bot=bot)
    await on_draft_callback(_cb(f"draft:{d2}:reject"), conn=ready_conn, bot=bot)
    assert db.get_draft(ready_conn, d1)["status"] == "approved"
    assert db.get_draft(ready_conn, d2)["status"] == "rejected"


@pytest.mark.asyncio
async def test_reject_callback_edits_card(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:reject"
    cb.message = AsyncMock()
    cb.message.text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot)

    assert db.get_draft(ready_conn, did)["status"] == "rejected"
    edit_call = cb.message.edit_text.await_args
    assert edit_call.kwargs["reply_markup"] is None
    assert "❌ Отклонено" in edit_call.kwargs["text"]


@pytest.mark.asyncio
async def test_approve_callback_edits_card(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:approve"
    cb.message = AsyncMock()
    cb.message.text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot)

    assert db.get_draft(ready_conn, did)["status"] == "approved"
    edit_call = cb.message.edit_text.await_args
    assert edit_call.kwargs["reply_markup"] is None
    assert edit_call.kwargs["text"].endswith("⏳ Отправляю…")


def test_set_draft_status_if_guards_transition(ready_conn):
    did = db.create_draft(ready_conn, 777, "a", "approved")

    ok = db.set_draft_status_if(ready_conn, did, "awaiting", "sent")
    assert ok is False
    assert db.get_draft(ready_conn, did)["status"] == "approved"

    ok = db.set_draft_status_if(ready_conn, did, "approved", "sent")
    assert ok is True
    assert db.get_draft(ready_conn, did)["status"] == "sent"


def test_supersede_awaiting_skips_already_transitioned_row(ready_conn):
    old_id = db.create_draft(ready_conn, 777, "old", "awaiting")
    new_id = db.create_draft(ready_conn, 777, "new", "awaiting")
    # старый черновик конкурентно уже сменил статус (например, владелец успел ответить)
    db.set_draft_status(ready_conn, old_id, "approved")

    superseded = db.supersede_awaiting(ready_conn, 777, new_id)

    assert superseded == []
    assert db.get_draft(ready_conn, old_id)["status"] == "approved"


def test_supersede_awaiting_ignores_newer_drafts(ready_conn):
    id_a = db.create_draft(ready_conn, 777, "a", "awaiting")
    id_b = db.create_draft(ready_conn, 777, "b", "awaiting")  # id_b > id_a

    superseded = db.supersede_awaiting(ready_conn, 777, id_a)

    assert superseded == []
    assert db.get_draft(ready_conn, id_b)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_approve_guard_rejects_when_status_changed_before_write(
    ready_conn, settings, monkeypatch  # noqa: F811
):
    # Симулирует TOCTOU: callback читает черновик как 'awaiting' (снапшот устарел),
    # но к моменту гвардированной записи статус уже сменился на 'superseded'.
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    stale_snapshot = dict(db.get_draft(ready_conn, did))
    monkeypatch.setattr(db, "get_draft", lambda conn, draft_id: stale_snapshot)
    ready_conn.execute("UPDATE drafts SET status='superseded' WHERE id=?", (did,))
    ready_conn.commit()

    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:approve"
    cb.message = AsyncMock()
    cb.message.text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot)

    row = ready_conn.execute("SELECT status FROM drafts WHERE id=?", (did,)).fetchone()
    assert row["status"] == "superseded"
    cb.answer.assert_awaited_once_with("Черновик уже неактуален")
    # approve правит карточку в ⏳ ДО гвардированного флипа (защита от гонки с вотчером),
    # а при неудавшемся флипе возвращает карточке актуальное состояние
    edits = cb.message.edit_text.await_args_list
    assert len(edits) == 2
    assert edits[0].kwargs["text"].endswith("⏳ Отправляю…")
    assert edits[1].kwargs["text"].endswith("⏭ Черновик уже неактуален")


def test_recover_stale_sending_flips_only_sending(ready_conn):
    d_sending1 = db.create_draft(ready_conn, 777, "a", "sending")
    d_sending2 = db.create_draft(ready_conn, 777, "b", "sending")
    d_approved = db.create_draft(ready_conn, 777, "c", "approved")
    d_pending = db.create_draft(ready_conn, 777, "d", "pending")

    count = db.recover_stale_sending(ready_conn)

    assert count == 2
    assert db.get_draft(ready_conn, d_sending1)["status"] == "approved"
    assert db.get_draft(ready_conn, d_sending2)["status"] == "approved"
    assert db.get_draft(ready_conn, d_approved)["status"] == "approved"
    assert db.get_draft(ready_conn, d_pending)["status"] == "pending"


@pytest.mark.asyncio
async def test_oversized_draft_fails_without_api_call(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "x" * 4097, "approved")
    db.set_draft_card(ready_conn, did, 444)
    bot = AsyncMock()
    await process_new_drafts(bot, ready_conn, settings)

    row = db.get_draft(ready_conn, did)
    assert row["status"] == "failed"
    assert "4096" in row["error"]
    bot.send_message.assert_not_called()  # API не вызывается для заведомо слишком длинного текста
    edit_call = bot.edit_message_text.await_args
    assert edit_call.kwargs["message_id"] == 444
    assert "⚠️ Не удалось отправить" in edit_call.kwargs["text"]


def test_card_text_bounded_with_long_contact_name():
    from tg_business_bridge.daemon.draft_handlers import _card_text

    draft = {"chat_id": 777, "text": "y" * 5000}
    card = _card_text(draft, contact="Ы" * 4000)
    assert len(card) <= 3500
    assert "обрезано" in card


@pytest.mark.asyncio
async def test_card_text_truncated_for_long_draft(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "y" * 5000, "pending")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 999
    await process_new_drafts(bot, ready_conn, settings)

    call = bot.send_message.await_args
    card_text = call.kwargs["text"]
    assert len(card_text) <= 3500
    assert "обрезано" in card_text


@pytest.mark.asyncio
async def test_malformed_callback_data_ignored(ready_conn, settings):  # noqa: F811
    d1 = db.create_draft(ready_conn, 777, "a", "awaiting")
    d2 = db.create_draft(ready_conn, 777, "b", "awaiting")
    bot = AsyncMock()

    def _cb(data):
        cb = AsyncMock()
        cb.data = data
        return cb

    # Test non-digit draft_id
    await on_draft_callback(_cb("draft:abc:approve"), conn=ready_conn, bot=bot)
    assert db.get_draft(ready_conn, d1)["status"] == "awaiting"

    # Test invalid action
    await on_draft_callback(_cb(f"draft:{d2}:destroy"), conn=ready_conn, bot=bot)
    assert db.get_draft(ready_conn, d2)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_no_connection_keeps_drafts_waiting(conn, settings):  # noqa: F811
    # без активного connection черновики не помечаются failed, а ждут его появления
    pid = db.create_draft(conn, 777, "карточка", "pending")
    aid = db.create_draft(conn, 777, "ответ", "approved")
    bot = AsyncMock()
    await process_new_drafts(bot, conn, settings)
    assert db.get_draft(conn, pid)["status"] == "pending"
    assert db.get_draft(conn, aid)["status"] == "approved"
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_poison_card_does_not_block_approved(ready_conn, settings):  # noqa: F811
    # сбой отправки карточки (например, владелец не открыл чат с ботом) не должен
    # блокировать ни другие карточки, ни очередь approved-черновиков
    p1 = db.create_draft(ready_conn, 777, "карточка", "pending")
    a1 = db.create_draft(ready_conn, 777, "ответ", "approved")
    bot = AsyncMock()

    async def side_effect(*args, **kwargs):
        if kwargs.get("reply_markup") is not None:  # карточка владельцу
            raise RuntimeError("bot can't initiate conversation")
        msg = AsyncMock()
        msg.message_id = 91
        return msg

    bot.send_message.side_effect = side_effect
    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, p1)["status"] == "pending"  # будет повторено
    assert db.get_draft(ready_conn, a1)["status"] == "sent"  # очередь не встала


@pytest.mark.asyncio
async def test_unexpected_send_error_marks_failed_not_sending(ready_conn, settings, monkeypatch):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "ответ", "approved")

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("tg_business_bridge.daemon.draft_handlers.send_business_reply", boom)
    await process_new_drafts(AsyncMock(), ready_conn, settings)
    row = db.get_draft(ready_conn, did)
    assert row["status"] == "failed" and "boom" in row["error"]  # не завис в 'sending'


@pytest.mark.asyncio
async def test_card_flood_wait_pauses_card_sending(ready_conn, settings, monkeypatch):  # noqa: F811
    from aiogram.exceptions import TelegramRetryAfter

    import tg_business_bridge.daemon.draft_handlers as dh

    monkeypatch.setattr(dh, "_flood_wait_until", 0.0)
    d1 = db.create_draft(ready_conn, 777, "a", "pending")
    d2 = db.create_draft(ready_conn, 777, "b", "pending")
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=AsyncMock(), message="flood", retry_after=60
    )
    await process_new_drafts(bot, ready_conn, settings)
    assert db.get_draft(ready_conn, d1)["status"] == "pending"
    assert db.get_draft(ready_conn, d2)["status"] == "pending"
    bot.send_message.assert_awaited_once()  # после первого flood-ответа попытки прекращаются

    bot.send_message.reset_mock()
    await process_new_drafts(bot, ready_conn, settings)
    bot.send_message.assert_not_called()  # пауза ещё не истекла


@pytest.mark.asyncio
async def test_card_contact_is_last_incoming_sender(ready_conn, settings):  # noqa: F811
    # имя в карточке — отправитель ПОСЛЕДНЕГО входящего, а не первого сообщения чата
    db.insert_message(ready_conn, _msg(message_id=11, ts=2000, direction="out", sender_name="Owner"))
    db.insert_message(ready_conn, _msg(message_id=12, ts=3000, sender_name="Новое Имя"))
    did = db.create_draft(ready_conn, 777, "черновик", "pending")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 321
    await process_new_drafts(bot, ready_conn, settings)
    assert "Новое Имя" in bot.send_message.await_args.kwargs["text"]
    assert db.get_draft(ready_conn, did)["status"] == "awaiting"


def test_list_drafts_filter_and_order(conn):
    d1 = db.create_draft(conn, 777, "первый", "pending")
    d2 = db.create_draft(conn, 777, "второй", "awaiting")
    d3 = db.create_draft(conn, 888, "чужой чат", "pending")

    all_rows = db.list_drafts(conn)
    assert [r["id"] for r in all_rows] == [d3, d2, d1]  # новые первыми

    chat_rows = db.list_drafts(conn, chat_id=777)
    assert [r["id"] for r in chat_rows] == [d2, d1]

    limited = db.list_drafts(conn, limit=1)
    assert [r["id"] for r in limited] == [d3]


@pytest.mark.asyncio
async def test_new_pending_draft_supersedes_older_awaiting(ready_conn, settings):  # noqa: F811
    old_id = db.create_draft(ready_conn, 777, "старый черновик", "pending")
    bot = AsyncMock()

    async def send1(*args, **kwargs):
        msg = AsyncMock()
        msg.message_id = 111
        return msg

    bot.send_message.side_effect = send1
    await process_new_drafts(bot, ready_conn, settings)
    assert db.get_draft(ready_conn, old_id)["status"] == "awaiting"

    new_id = db.create_draft(ready_conn, 777, "новый черновик", "pending")

    async def send2(*args, **kwargs):
        msg = AsyncMock()
        msg.message_id = 222
        return msg

    bot.send_message.side_effect = send2
    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, old_id)["status"] == "superseded"
    assert db.get_draft(ready_conn, new_id)["status"] == "awaiting"
    edit_call = bot.edit_message_text.await_args
    assert edit_call.kwargs["message_id"] == 111
    assert "⏭ Заменён новым черновиком" in edit_call.kwargs["text"]
    assert edit_call.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_supersede_does_not_cross_chats(ready_conn, settings):  # noqa: F811
    db.insert_message(ready_conn, _msg(message_id=20, chat_id=888, direction="in"))
    id_a = db.create_draft(ready_conn, 777, "a", "pending")
    id_b = db.create_draft(ready_conn, 888, "b", "pending")
    bot = AsyncMock()
    counter = {"n": 0}

    async def send_side_effect(*args, **kwargs):
        counter["n"] += 1
        msg = AsyncMock()
        msg.message_id = counter["n"]
        return msg

    bot.send_message.side_effect = send_side_effect
    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, id_a)["status"] == "awaiting"
    assert db.get_draft(ready_conn, id_b)["status"] == "awaiting"
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_on_superseded_draft_leaves_status(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "заменённый", "superseded")
    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:approve"

    await on_draft_callback(cb, conn=ready_conn, bot=bot)

    assert db.get_draft(ready_conn, did)["status"] == "superseded"
    cb.answer.assert_awaited_once_with("Черновик уже неактуален")
    cb.message.edit_text.assert_not_awaited()
