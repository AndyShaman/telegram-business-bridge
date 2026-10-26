import json
import sqlite3
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from aiogram.types import FSInputFile, ReplyKeyboardRemove

from tg_business_bridge import db, media
from tg_business_bridge.daemon.draft_handlers import (
    _card_markup,
    _card_text,
    on_draft_callback,
    on_editor_result,
    on_paused_command,
    on_unpause_callback,
    process_new_drafts,
)
from test_db import _msg
from test_business_handlers import settings  # noqa: F401 - фикстура


@pytest.fixture()
def ready_conn(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": true}', True)
    db.insert_message(conn, _msg(message_id=10, direction="in"))
    return conn


def _webapp_msg(user_id: int, data: str):
    msg = AsyncMock()
    msg.from_user.id = user_id
    msg.web_app_data.data = data
    return msg


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
    assert kb[1].callback_data == f"draft:{did}:edit"


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

    await on_draft_callback(_cb(f"draft:{d1}:approve"), conn=ready_conn, bot=bot, settings=settings)
    await on_draft_callback(_cb(f"draft:{d2}:reject"), conn=ready_conn, bot=bot, settings=settings)
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
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

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
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

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
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

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

    draft = {"chat_id": 777, "text": "y" * 5000, "parse_mode": None}
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
    await on_draft_callback(_cb("draft:abc:approve"), conn=ready_conn, bot=bot, settings=settings)
    assert db.get_draft(ready_conn, d1)["status"] == "awaiting"

    # Test invalid action
    await on_draft_callback(_cb(f"draft:{d2}:destroy"), conn=ready_conn, bot=bot, settings=settings)
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

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    assert db.get_draft(ready_conn, did)["status"] == "superseded"
    cb.answer.assert_awaited_once_with("Черновик уже неактуален")
    cb.message.edit_text.assert_not_awaited()


def test_update_draft_text_only_on_awaiting(ready_conn):
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")

    assert db.update_draft_text(ready_conn, did, "новый") is True
    assert db.get_draft(ready_conn, did)["text"] == "новый"

    for status in ("pending", "approved", "sending", "sent", "rejected", "superseded"):
        other = db.create_draft(ready_conn, 777, "исходный", status)
        assert db.update_draft_text(ready_conn, other, "правка") is False
        assert db.get_draft(ready_conn, other)["text"] == "исходный"


def test_card_markup_without_editor_url_has_no_edit_button(settings):  # noqa: F811
    settings.editor_url = ""
    kb = _card_markup(7, settings).inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["draft:7:approve"]


@pytest.mark.asyncio
async def test_edit_callback_offers_editor_button(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "текст с пробелами", "awaiting")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 501
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"
    cb.message.chat.id = 42

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["status"] == "awaiting"  # редактирование не меняет статус
    assert row["edit_prompt_message_id"] == 501
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 42
    button = call.kwargs["reply_markup"].keyboard[0][0]
    assert button.web_app.url == (
        f"{settings.editor_url}#id={did}&html={quote('текст с пробелами')}"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_edit_callback_replaces_previous_prompt(ready_conn, settings):  # noqa: F811
    # повторное «Редактировать» не должно копить приглашения в диалоге
    did = db.create_draft(ready_conn, 777, "черновик", "awaiting")
    db.set_edit_prompt(ready_conn, did, 500)
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 600
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"
    cb.message.chat.id = 42

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    assert bot.delete_message.await_args.kwargs["message_id"] == 500
    assert db.get_draft(ready_conn, did)["edit_prompt_message_id"] == 600


@pytest.mark.asyncio
async def test_edit_callback_without_editor_url_answers(ready_conn, settings):  # noqa: F811
    # карточка с кнопкой могла быть разослана до того, как редактор выключили
    did = db.create_draft(ready_conn, 777, "черновик", "awaiting")
    settings.editor_url = ""
    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    bot.send_message.assert_not_called()
    cb.answer.assert_awaited_once_with("Редактор недоступен")
    assert db.get_draft(ready_conn, did)["status"] == "awaiting"
    assert db.get_draft(ready_conn, did)["text"] == "черновик"


@pytest.mark.asyncio
async def test_edit_callback_send_failure_answers_callback(ready_conn, settings):  # noqa: F811
    # без ответа на callback пользователь видел бы вечный спиннер
    did = db.create_draft(ready_conn, 777, "черновик", "awaiting")
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("BUTTON_URL_INVALID")
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    assert "Не удалось открыть редактор" in cb.answer.await_args.args[0]
    cb.answer.assert_awaited_once()
    assert db.get_draft(ready_conn, did)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_edit_callback_on_stale_draft_does_nothing(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "уже отправлен", "sent")
    bot = AsyncMock()
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    cb.answer.assert_awaited_once_with("Черновик уже обработан")
    bot.send_message.assert_not_called()


def _sending_bot(*message_ids: int):
    """Бот, чьи send_message отдают заданные message_id по порядку вызовов."""
    bot = AsyncMock()
    ids = iter(message_ids)

    async def send(*args, **kwargs):
        sent = AsyncMock()
        sent.message_id = next(ids)
        return sent

    bot.send_message.side_effect = send
    return bot


@pytest.mark.asyncio
async def test_editor_result_reposts_card_and_cleans_up(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    db.set_edit_prompt(ready_conn, did, 222)
    bot = _sending_bot(901, 902)  # 901 — сообщение-«ножницы», 902 — новая карточка
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "исправленный"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["text"] == "исправленный"
    assert row["status"] == "awaiting"  # статус не трогаем
    assert row["edit_prompt_message_id"] is None
    assert row["card_message_id"] == 902  # карточка пересоздана внизу диалога

    msg.delete.assert_awaited_once()  # плашка «данные переданы боту»
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted == [222, 901, 111]  # приглашение, «ножницы», старая карточка

    stub, card = bot.send_message.await_args_list
    assert stub.kwargs["text"] == "✂️"
    assert isinstance(stub.kwargs["reply_markup"], ReplyKeyboardRemove)
    assert card.kwargs["chat_id"] == 42
    assert "исправленный" in card.kwargs["text"]
    kb = card.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == [f"draft:{did}:approve", f"draft:{did}:edit"]
    msg.answer.assert_not_awaited()  # подтверждение — сама свежая карточка


@pytest.mark.asyncio
async def test_editor_result_without_prompt_id_still_drops_keyboard(ready_conn, settings):  # noqa: F811
    # id приглашения мог не сохраниться (черновик застал обновление в процессе правки),
    # но клавиатура на экране точно есть — раз пришли данные редактора
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    bot = _sending_bot(901, 902)
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "исправленный"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    stub, card = bot.send_message.await_args_list
    assert stub.kwargs["text"] == "✂️"
    assert isinstance(stub.kwargs["reply_markup"], ReplyKeyboardRemove)
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted == [901]  # «ножницы» отправлены и убраны, больше удалять нечего
    assert card.kwargs["chat_id"] == 42
    assert db.get_draft(ready_conn, did)["card_message_id"] == 902
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_editor_result_undeletable_card_is_marked_replaced(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    bot = _sending_bot(901, 902)

    async def delete(chat_id, message_id):
        if message_id == 111:  # Telegram не даёт удалять сообщения старше 48 часов
            raise RuntimeError("message can't be deleted")

    bot.delete_message.side_effect = delete
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "исправленный"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    edit_call = bot.edit_message_text.await_args
    assert edit_call.kwargs["message_id"] == 111
    assert edit_call.kwargs["text"].endswith("⏭ Заменён обновлённой карточкой")
    assert edit_call.kwargs["reply_markup"] is None
    assert db.get_draft(ready_conn, did)["card_message_id"] == 902  # новая карточка всё равно ушла


@pytest.mark.asyncio
async def test_editor_result_card_send_failure_keeps_old_card(ready_conn, settings):  # noqa: F811
    # если новая карточка не ушла, старая должна остаться рабочей — иначе черновик
    # остался бы в awaiting вообще без кнопок
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    bot = AsyncMock()
    sends = {"n": 0}

    async def send(*args, **kwargs):
        sends["n"] += 1
        if sends["n"] == 1:  # «ножницы» уходят, падает уже карточка
            stub = AsyncMock()
            stub.message_id = 901
            return stub
        raise RuntimeError("bot was blocked by the user")

    bot.send_message.side_effect = send
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "исправленный"}))

    with pytest.raises(RuntimeError):
        await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["text"] == "исправленный"  # правка сохранена
    assert row["card_message_id"] == 111  # карточка в БД прежняя
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted == [901]  # удалены только «ножницы», старая карточка на месте


@pytest.mark.asyncio
async def test_approve_callback_cleans_up_editor_prompt(ready_conn, settings):  # noqa: F811
    # владелец открыл редактор, но передумал и отправил черновик прямо с карточки —
    # приглашение с клавиатурой не должно остаться висеть в чате
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    db.set_edit_prompt(ready_conn, did, 222)
    bot = _sending_bot(901)  # «ножницы»
    cb = AsyncMock()
    cb.data = f"draft:{did}:approve"
    cb.message.chat.id = 42
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["status"] == "approved"
    assert row["edit_prompt_message_id"] is None
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted == [222, 901]  # приглашение и следом «ножницы»


@pytest.mark.asyncio
async def test_reject_callback_cleans_up_editor_prompt(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_edit_prompt(ready_conn, did, 222)
    bot = _sending_bot(901)
    cb = AsyncMock()
    cb.data = f"draft:{did}:reject"
    cb.message.chat.id = 42
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    assert db.get_draft(ready_conn, did)["edit_prompt_message_id"] is None
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted == [222, 901]


@pytest.mark.asyncio
async def test_stale_draft_callback_leaves_editor_prompt(ready_conn, settings, monkeypatch):  # noqa: F811
    # флип не удался — черновик уже неактуален, ничего дополнительно не убираем
    did = db.create_draft(ready_conn, 777, "b", "awaiting")
    db.set_edit_prompt(ready_conn, did, 222)
    stale_snapshot = dict(db.get_draft(ready_conn, did))
    monkeypatch.setattr(db, "get_draft", lambda conn, draft_id: stale_snapshot)
    ready_conn.execute("UPDATE drafts SET status='superseded' WHERE id=?", (did,))
    ready_conn.commit()
    bot = _sending_bot(901)
    cb = AsyncMock()
    cb.data = f"draft:{did}:reject"
    cb.message.chat.id = 42
    cb.message.html_text = "Черновик ответа для X (chat 777):\n\nb"

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    bot.delete_message.assert_not_awaited()
    row = ready_conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    assert row["edit_prompt_message_id"] == 222


@pytest.mark.asyncio
async def test_editor_result_from_stranger_ignored(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    bot = AsyncMock()
    msg = _webapp_msg(999, json.dumps({"id": str(did), "html": "чужая правка"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    assert db.get_draft(ready_conn, did)["text"] == "старый"
    msg.answer.assert_not_awaited()
    msg.delete.assert_not_awaited()  # чужие сообщения не трогаем
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_editor_result_garbage_leaves_draft(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    bot = AsyncMock()
    payloads = [
        "не json",
        json.dumps({"id": str(did)}),                      # нет текста
        json.dumps({"html": "правка"}),                    # нет id
        json.dumps({"id": "abc", "html": "правка"}),       # id не число
        json.dumps({"id": str(did), "html": "   "}),       # пустой текст
        json.dumps({"id": str(did), "html": "<b></b>"}),   # разметка без видимого текста
        json.dumps({"id": str(did), "html": 5}),           # текст не строка
        json.dumps({"id": str(did), "html": "x" * 4097}),  # длиннее лимита Telegram
        json.dumps(["id", "html"]),                        # не объект
    ]
    for payload in payloads:
        msg = _webapp_msg(42, payload)
        await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)
        assert msg.answer.await_args.args[0] == "Не удалось обработать данные редактора"
        msg.delete.assert_awaited_once()  # плашка убирается при любом исходе

    assert db.get_draft(ready_conn, did)["text"] == "старый"


@pytest.mark.asyncio
async def test_editor_result_on_non_awaiting_draft_rejected(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "approved")
    bot = AsyncMock()
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "поздняя правка"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    assert db.get_draft(ready_conn, did)["text"] == "старый"
    answer = msg.answer.await_args
    assert answer.args[0] == "Черновик уже неактуален"
    assert isinstance(answer.kwargs["reply_markup"], ReplyKeyboardRemove)
    msg.delete.assert_awaited_once()
    bot.send_message.assert_not_called()  # карточку не пересоздаём


@pytest.mark.asyncio
async def test_editor_result_stale_draft_still_removes_prompt(ready_conn, settings):  # noqa: F811
    # черновик ушёл из awaiting, пока редактор был открыт: приглашение к нему
    # больше никто не уберёт — остальные пути уборки стоят за гвардом awaiting
    did = db.create_draft(ready_conn, 777, "старый", "superseded")
    db.set_edit_prompt(ready_conn, did, 222)
    bot = AsyncMock()
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "поздняя правка"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["text"] == "старый"
    assert row["edit_prompt_message_id"] is None
    assert bot.delete_message.await_args.kwargs["message_id"] == 222
    answer = msg.answer.await_args
    assert answer.args[0] == "Черновик уже неактуален"
    assert isinstance(answer.kwargs["reply_markup"], ReplyKeyboardRemove)


_OLD_DRAFTS_SCHEMA = """CREATE TABLE drafts (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_ts INTEGER NOT NULL,
    card_message_id INTEGER)"""


def test_init_schema_adds_edit_prompt_column(tmp_path):
    raw = sqlite3.connect(tmp_path / "old.db")
    raw.execute(_OLD_DRAFTS_SCHEMA)
    raw.commit()

    db.init_schema(raw)
    db.init_schema(raw)  # повторный старт демона не должен падать

    cols = {r[1] for r in raw.execute("PRAGMA table_info(drafts)")}
    assert "edit_prompt_message_id" in cols
    assert "parse_mode" in cols
    raw.close()


class _ProxyConnection:
    """Вклинивается в шаги миграции: у sqlite3.Connection атрибуты read-only,
    подменить execute на самом соединении нельзя."""

    def __init__(self, conn, after_pragma=None, alter_error=None):
        self._conn = conn
        self._after_pragma = after_pragma
        self._alter_error = alter_error

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def execute(self, sql, *args):
        if self._alter_error is not None and sql.startswith("ALTER TABLE"):
            raise self._alter_error
        cur = self._conn.execute(sql, *args)
        if self._after_pragma is not None and sql.startswith("PRAGMA table_info"):
            rows = cur.fetchall()
            self._after_pragma()
            return rows
        return cur

    def commit(self):
        return self._conn.commit()


def test_init_schema_survives_concurrent_migration(tmp_path):
    # демон и MCP-сервер стартуют независимо: колонку мог добавить сосед
    # между нашими PRAGMA и ALTER
    path = tmp_path / "race.db"
    primary = sqlite3.connect(path)
    rival = sqlite3.connect(path)
    primary.execute(_OLD_DRAFTS_SCHEMA)
    primary.commit()

    def rival_migrates():
        rival.execute("ALTER TABLE drafts ADD COLUMN edit_prompt_message_id INTEGER")
        rival.commit()

    db.init_schema(_ProxyConnection(primary, after_pragma=rival_migrates))

    cols = {r[1] for r in primary.execute("PRAGMA table_info(drafts)")}
    assert "edit_prompt_message_id" in cols
    primary.close()
    rival.close()


def test_init_schema_reraises_other_operational_errors(tmp_path):
    raw = sqlite3.connect(tmp_path / "broken.db")
    raw.execute(_OLD_DRAFTS_SCHEMA)
    raw.commit()
    locked = sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        db.init_schema(_ProxyConnection(raw, alter_error=locked))
    raw.close()


_PRE_HTML_DRAFTS_SCHEMA = """CREATE TABLE drafts (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_ts INTEGER NOT NULL,
    card_message_id INTEGER, edit_prompt_message_id INTEGER)"""


def test_init_schema_adds_parse_mode_to_existing_db(tmp_path):
    # рабочая БД моста: редактор уже был, разметки ещё нет
    raw = sqlite3.connect(tmp_path / "pre_html.db")
    raw.execute(_PRE_HTML_DRAFTS_SCHEMA)
    raw.execute(
        "INSERT INTO drafts (chat_id, text, status, created_ts) VALUES (777, 'старый', 'awaiting', 1)"
    )
    raw.commit()

    db.init_schema(raw)
    db.init_schema(raw)  # повторный старт демона не должен падать

    cols = [r[1] for r in raw.execute("PRAGMA table_info(drafts)")]
    assert "parse_mode" in cols
    row = raw.execute("SELECT * FROM drafts").fetchone()
    assert row[cols.index("parse_mode")] is None  # старые черновики остаются обычным текстом
    raw.close()


def test_create_and_update_draft_carry_parse_mode(ready_conn):
    did = db.create_draft(ready_conn, 777, "<b>привет</b>", "awaiting", "HTML")
    assert db.get_draft(ready_conn, did)["parse_mode"] == "HTML"

    plain = db.create_draft(ready_conn, 888, "привет", "awaiting")
    assert db.get_draft(ready_conn, plain)["parse_mode"] is None

    assert db.update_draft_text(ready_conn, plain, '<i>правка</i>', "HTML") is True
    row = db.get_draft(ready_conn, plain)
    assert row["text"] == "<i>правка</i>" and row["parse_mode"] == "HTML"

    # правка без разметки сбрасывает parse_mode обратно
    assert db.update_draft_text(ready_conn, did, "просто текст") is True
    assert db.get_draft(ready_conn, did)["parse_mode"] is None


@pytest.mark.asyncio
async def test_html_draft_card_keeps_markup(ready_conn, settings):  # noqa: F811
    link = '<a href="https://example.com/page">по ссылке</a>'
    db.create_draft(ready_conn, 777, f"Подробности {link}", "pending", "HTML")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 555

    await process_new_drafts(bot, ready_conn, settings)

    call = bot.send_message.await_args
    assert call.kwargs["parse_mode"] == "HTML"
    assert link in call.kwargs["text"]  # разметка не экранирована


@pytest.mark.asyncio
async def test_plain_draft_card_escapes_markup(ready_conn, settings):  # noqa: F811
    db.create_draft(ready_conn, 777, "1 < 2 & 3", "pending")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 555

    await process_new_drafts(bot, ready_conn, settings)

    call = bot.send_message.await_args
    assert call.kwargs["parse_mode"] == "HTML"
    assert "1 &lt; 2 &amp; 3" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_html_draft_is_sent_with_parse_mode(ready_conn, settings):  # noqa: F811
    html = '<b>привет</b>'
    db.create_draft(ready_conn, 777, html, "approved", "HTML")
    bot = AsyncMock()

    await process_new_drafts(bot, ready_conn, settings)

    call = bot.send_message.await_args
    assert call.kwargs["text"] == html
    assert call.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_long_html_draft_card_not_cut_inside_tag(ready_conn, settings):  # noqa: F811
    db.create_draft(ready_conn, 777, "<b>" + "я" * 5000 + "</b>", "pending", "HTML")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 555

    await process_new_drafts(bot, ready_conn, settings)

    card = bot.send_message.await_args.kwargs["text"]
    assert len(card) <= 3500
    assert "обрезано" in card
    assert "<" not in card  # обрезанное тело уходит без разметки, а не с рваным тегом


@pytest.mark.asyncio
async def test_oversize_counted_by_visible_text(ready_conn, settings):  # noqa: F811
    # длинный href не мешает: лимит Telegram считается по видимому тексту
    href = "https://example.com/" + "x" * 5000
    did = db.create_draft(ready_conn, 777, f'<a href="{href}">коротко</a>', "approved", "HTML")
    bot = AsyncMock()

    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, did)["status"] == "sent"


@pytest.mark.asyncio
async def test_edit_callback_url_carries_escaped_plain_text(ready_conn, settings):  # noqa: F811
    # редактор всегда получает HTML — обычный текст экранируется заранее
    did = db.create_draft(ready_conn, 777, "1 < 2", "awaiting")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 501
    cb = AsyncMock()
    cb.data = f"draft:{did}:edit"
    cb.message.chat.id = 42

    await on_draft_callback(cb, conn=ready_conn, bot=bot, settings=settings)

    button = bot.send_message.await_args.kwargs["reply_markup"].keyboard[0][0]
    assert button.web_app.url == f"{settings.editor_url}#id={did}&html={quote('1 &lt; 2')}"


@pytest.mark.asyncio
async def test_editor_result_marks_draft_html(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    bot = _sending_bot(901, 902)
    edited = 'смотри <a href="https://example.com">тут</a>'
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": edited}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["text"] == edited
    assert row["parse_mode"] == "HTML"
    card = bot.send_message.await_args_list[1]
    assert card.kwargs["parse_mode"] == "HTML"
    assert edited in card.kwargs["text"]


@pytest.mark.asyncio
async def test_editor_result_accepts_long_href_with_short_text(ready_conn, settings):  # noqa: F811
    # видимого текста мало, но исходник длиннее 4096 — правка должна пройти
    did = db.create_draft(ready_conn, 777, "старый", "awaiting")
    href = "https://example.com/" + "x" * 5000
    bot = _sending_bot(901, 902)
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": f'<a href="{href}">тут</a>'}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    assert db.get_draft(ready_conn, did)["parse_mode"] == "HTML"
    assert href in db.get_draft(ready_conn, did)["text"]


@pytest.mark.asyncio
async def test_broken_html_card_marks_draft_failed(ready_conn, settings):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    # битая разметка от агента: повторять отправку карточки бессмысленно
    did = db.create_draft(ready_conn, 777, "<b>незакрытый", "pending", "HTML")
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(
        method=AsyncMock(), message="can't parse entities: Unclosed start tag"
    )

    await process_new_drafts(bot, ready_conn, settings)

    row = db.get_draft(ready_conn, did)
    assert row["status"] == "failed"
    assert "Unclosed start tag" in row["error"]

    bot.send_message.reset_mock()
    await process_new_drafts(bot, ready_conn, settings)
    bot.send_message.assert_not_called()  # черновик закрыт, демон его больше не берёт


def test_parse_editor_payload_accepts_both_protocols():
    from tg_business_bridge.daemon.draft_handlers import _parse_editor_payload

    assert _parse_editor_payload(json.dumps({"id": "7", "html": "<b>x</b>"})) == (
        7, "<b>x</b>", "HTML"
    )
    # страница старой версии присылает text — её результат остаётся обычным текстом
    assert _parse_editor_payload(json.dumps({"id": "7", "text": "1 < 2"})) == (7, "1 < 2", None)
    assert _parse_editor_payload(json.dumps({"id": "7"})) is None


@pytest.mark.asyncio
async def test_editor_result_legacy_text_keeps_plain(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "старый", "awaiting", "HTML")
    bot = _sending_bot(901, 902)
    msg = _webapp_msg(42, json.dumps({"id": str(did), "text": "1 < 2 и всё"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)

    row = db.get_draft(ready_conn, did)
    assert row["text"] == "1 < 2 и всё"
    assert row["parse_mode"] is None  # правка со старой страницы снимает разметку
    card = bot.send_message.await_args_list[1]
    assert "1 &lt; 2 и всё" in card.kwargs["text"]  # в карточке текст экранирован


# --- вложения, пауза ---


def _draft_with_upload(conn, settings, status, name="отчёт.pdf", data=b"%PDF-1", text="вот файл"):  # noqa: F811
    """Черновик с загруженным агентом файлом: запись в БД + файл в outbox."""
    def materialize(draft_id):
        d = media.outbox_dir(settings.data_dir, draft_id)
        d.mkdir(parents=True)
        (d / f"0-{name}").write_bytes(data)
        return [{"kind": "document", "filename": name, "file_size": len(data),
                 "path": str((d / f"0-{name}").relative_to(settings.data_dir))}]
    return db.create_draft(conn, 777, text, status, None, materialize)


@pytest.mark.asyncio
async def test_upload_sent_as_fsinputfile_and_outbox_removed(ready_conn, settings):  # noqa: F811
    did = _draft_with_upload(ready_conn, settings, "approved")
    outbox = media.outbox_dir(settings.data_dir, did)
    assert outbox.exists()
    bot = AsyncMock()
    bot.id = 99

    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, did)["status"] == "sent"
    kw = bot.send_document.await_args.kwargs
    assert isinstance(kw["document"], FSInputFile)
    assert kw["document"].filename == "отчёт.pdf"  # уходит под исходным именем
    assert kw["caption"] == "вот файл"
    assert not outbox.exists()


@pytest.mark.asyncio
async def test_archive_attachment_sent_by_file_id(ready_conn, settings):  # noqa: F811
    def materialize(draft_id):
        return [{"kind": "photo", "file_id": "FID1", "filename": None, "file_size": 10}]
    db.create_draft(ready_conn, 777, "", "approved", None, materialize)
    bot = AsyncMock()
    bot.id = 99
    await process_new_drafts(bot, ready_conn, settings)
    assert bot.send_photo.await_args.kwargs["photo"] == "FID1"


@pytest.mark.asyncio
async def test_failed_send_cleans_outbox(ready_conn, settings):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    did = _draft_with_upload(ready_conn, settings, "approved")
    bot = AsyncMock()
    bot.send_document.side_effect = TelegramBadRequest(method=AsyncMock(), message="boom")

    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, did)["status"] == "failed"
    assert not media.outbox_dir(settings.data_dir, did).exists()


@pytest.mark.asyncio
async def test_card_badrequest_cleans_outbox(ready_conn, settings):  # noqa: F811
    from aiogram.exceptions import TelegramBadRequest

    did = _draft_with_upload(ready_conn, settings, "pending")
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(method=AsyncMock(), message="can't parse")

    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, did)["status"] == "failed"
    assert not media.outbox_dir(settings.data_dir, did).exists()


@pytest.mark.asyncio
async def test_supersede_cleans_outbox_and_old_card_keeps_attachment_line(
    ready_conn, settings,  # noqa: F811
):
    old = _draft_with_upload(ready_conn, settings, "awaiting")
    db.set_draft_card(ready_conn, old, 111)
    new = db.create_draft(ready_conn, 777, "новый", "pending")
    bot = _sending_bot(555)

    await process_new_drafts(bot, ready_conn, settings)

    assert db.get_draft(ready_conn, old)["status"] == "superseded"
    assert db.get_draft(ready_conn, new)["status"] == "awaiting"
    assert not media.outbox_dir(settings.data_dir, old).exists()
    edited = bot.edit_message_text.await_args.kwargs
    assert edited["message_id"] == 111
    assert "📎 1 вложение: отчёт.pdf" in edited["text"]
    assert "Заменён новым черновиком" in edited["text"]


@pytest.mark.asyncio
async def test_reject_callback_cleans_outbox(ready_conn, settings):  # noqa: F811
    did = _draft_with_upload(ready_conn, settings, "awaiting")
    cb = AsyncMock()
    cb.data = f"draft:{did}:reject"
    cb.message.html_text = "карточка"

    await on_draft_callback(cb, conn=ready_conn, bot=AsyncMock(), settings=settings)

    assert db.get_draft(ready_conn, did)["status"] == "rejected"
    assert not media.outbox_dir(settings.data_dir, did).exists()


def _attachment(name, size=None, kind="document"):
    return {"kind": kind, "filename": name, "file_size": size}


def _plain_draft(text="привет"):
    return {"chat_id": 777, "text": text, "parse_mode": None}


def test_card_text_attachment_line_singular_with_size():
    text = _card_text(_plain_draft(), "Alice", [_attachment("a.pdf", 340 * 1024)])
    assert text.endswith("📎 1 вложение: a.pdf (340 KB)")


@pytest.mark.parametrize("n,word", [(1, "вложение"), (2, "вложения"), (4, "вложения"),
                                    (5, "вложений"), (10, "вложений")])
def test_card_text_attachment_plural(n, word):
    text = _card_text(_plain_draft(), "Alice", [_attachment(f"f{i}.txt") for i in range(n)])
    assert f"📎 {n} {word}: " in text


def test_card_text_attachment_name_is_escaped_and_without_size_when_unknown():
    text = _card_text(_plain_draft(), "Alice", [_attachment("<b>&.txt")])
    assert "📎 1 вложение: &lt;b&gt;&amp;.txt" in text
    assert "<b>&." not in text


def test_card_text_without_attachments_has_no_line():
    assert "📎" not in _card_text(_plain_draft(), "Alice", [])
    assert "📎" not in _card_text(_plain_draft(), "Alice")


def test_card_text_attachment_line_survives_truncation():
    atts = [_attachment(f"{'ф' * 70}{i}.pdf", 2 * 1024 * 1024) for i in range(10)]
    text = _card_text(_plain_draft("я" * 5000), "К" * 100, atts)
    assert len(text) <= 3500
    assert "обрезано" in text
    assert "📎 10 вложений: " in text
    assert text.index("обрезано") < text.index("📎")


@pytest.mark.asyncio
async def test_pending_card_shows_attachment_line(ready_conn, settings):  # noqa: F811
    _draft_with_upload(ready_conn, settings, "pending")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 5
    await process_new_drafts(bot, ready_conn, settings)
    assert "📎 1 вложение: отчёт.pdf" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_editor_result_keeps_attachments(ready_conn, settings):  # noqa: F811
    did = _draft_with_upload(ready_conn, settings, "awaiting")
    db.set_draft_card(ready_conn, did, 111)
    bot = _sending_bot(901, 902)
    msg = _webapp_msg(42, json.dumps({"id": str(did), "html": "правка"}))

    await on_editor_result(msg, conn=ready_conn, bot=bot, settings=settings)
    msg2 = _webapp_msg(42, json.dumps({"id": str(did), "html": "правка 2"}))
    bot2 = _sending_bot(903, 904)
    await on_editor_result(msg2, conn=ready_conn, bot=bot2, settings=settings)

    atts = db.get_draft_attachments(ready_conn, did)
    assert [a["filename"] for a in atts] == ["отчёт.pdf"]
    assert media.outbox_dir(settings.data_dir, did).exists()
    assert "📎 1 вложение" in bot2.send_message.await_args_list[-1].kwargs["text"]
    row = db.get_draft(ready_conn, did)
    assert row["original_text"] == "вот файл" and row["text"] == "правка 2"


def test_card_markup_has_pause_row(settings):  # noqa: F811
    kb = _card_markup(7, settings).inline_keyboard
    assert [b.callback_data for b in kb[0]] == ["draft:7:approve", "draft:7:edit"]
    assert [b.callback_data for b in kb[1]] == ["draft:7:pause"]


def test_card_markup_pause_row_without_editor(settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "editor_url", "")
    kb = _card_markup(7, settings).inline_keyboard
    assert [b.callback_data for b in kb[0]] == ["draft:7:approve"]
    assert [b.callback_data for b in kb[1]] == ["draft:7:pause"]


@pytest.mark.asyncio
async def test_pause_callback_rejects_pauses_and_cleans(ready_conn, settings):  # noqa: F811
    did = _draft_with_upload(ready_conn, settings, "awaiting")
    cb = AsyncMock()
    cb.data = f"draft:{did}:pause"
    cb.message.html_text = "карточка"

    await on_draft_callback(cb, conn=ready_conn, bot=AsyncMock(), settings=settings)

    assert db.get_draft(ready_conn, did)["status"] == "rejected"
    assert db.is_chat_paused(ready_conn, 777)
    assert not media.outbox_dir(settings.data_dir, did).exists()
    edit = cb.message.edit_text.await_args.kwargs
    assert edit["reply_markup"] is None and "/paused" in edit["text"]


@pytest.mark.asyncio
async def test_pause_callback_on_processed_draft_does_not_pause(ready_conn, settings):  # noqa: F811
    did = db.create_draft(ready_conn, 777, "x", "sent")
    cb = AsyncMock()
    cb.data = f"draft:{did}:pause"
    await on_draft_callback(cb, conn=ready_conn, bot=AsyncMock(), settings=settings)
    assert not db.is_chat_paused(ready_conn, 777)


def _user_msg(user_id):
    msg = AsyncMock()
    msg.from_user.id = user_id
    return msg


@pytest.mark.asyncio
async def test_paused_command_lists_chats_for_owner(ready_conn):
    db.pause_chat(ready_conn, 777)
    msg = _user_msg(42)
    await on_paused_command(msg, conn=ready_conn)
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Alice" in text and "777" in text
    kb = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "unpause:777"


@pytest.mark.asyncio
async def test_paused_command_empty_list(ready_conn):
    msg = _user_msg(42)
    await on_paused_command(msg, conn=ready_conn)
    assert "нет" in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_paused_command_ignores_stranger(ready_conn):
    db.pause_chat(ready_conn, 777)
    msg = _user_msg(999)
    await on_paused_command(msg, conn=ready_conn)
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpause_callback_owner_unpauses(ready_conn):
    db.pause_chat(ready_conn, 777)
    cb = AsyncMock()
    cb.data = "unpause:777"
    cb.from_user.id = 42
    await on_unpause_callback(cb, conn=ready_conn)
    assert not db.is_chat_paused(ready_conn, 777)
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_unpause_callback_ignores_stranger(ready_conn):
    db.pause_chat(ready_conn, 777)
    cb = AsyncMock()
    cb.data = "unpause:777"
    cb.from_user.id = 999
    await on_unpause_callback(cb, conn=ready_conn)
    assert db.is_chat_paused(ready_conn, 777)
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpause_callback_garbage_chat_id_ignored(ready_conn):
    db.pause_chat(ready_conn, 777)
    cb = AsyncMock()
    cb.data = "unpause:abc"
    cb.from_user.id = 42
    await on_unpause_callback(cb, conn=ready_conn)
    assert db.is_chat_paused(ready_conn, 777)
