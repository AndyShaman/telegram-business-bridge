import asyncio
import logging
import sqlite3

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from tg_business_bridge import db
from tg_business_bridge.config import Settings
from tg_business_bridge.sender import send_business_reply

log = logging.getLogger(__name__)
router = Router()


def _card_markup(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"draft:{draft_id}:approve"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"draft:{draft_id}:reject"),
    ]])


_CARD_LIMIT = 3500
_TRUNCATE_MARKER = "… [обрезано, полный текст будет отправлен]"


def _card_text(draft: sqlite3.Row, contact: str) -> str:
    # имя контакта приходит из Telegram и не ограничено схемой БД —
    # без обрезки длинное имя съедает лимит и карточка может превысить 4096
    if len(contact) > 64:
        contact = contact[:63] + "…"
    header = f"Черновик ответа для {contact} (chat {draft['chat_id']}):\n\n"
    body = header + draft["text"]
    if len(body) <= _CARD_LIMIT:
        return body
    limit = max(0, _CARD_LIMIT - len(header) - len(_TRUNCATE_MARKER))
    return header + draft["text"][:limit] + _TRUNCATE_MARKER


async def process_new_drafts(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> None:
    connection = db.get_enabled_connection(conn)
    for draft in db.get_drafts_by_status(conn, "pending"):
        if connection is None:
            db.set_draft_status(conn, draft["id"], "failed", "no active business connection")
            continue
        hist = db.get_history(conn, draft["chat_id"], limit=1)
        contact = hist[0]["sender_name"] if hist else str(draft["chat_id"])
        text = _card_text(draft, contact)
        sent = await bot.send_message(
            chat_id=connection["owner_id"], text=text, reply_markup=_card_markup(draft["id"])
        )
        db.set_draft_card(conn, draft["id"], sent.message_id)
        db.set_draft_status(conn, draft["id"], "awaiting")

        for old in db.supersede_awaiting(conn, draft["chat_id"], draft["id"]):
            if old["card_message_id"] is None:
                continue
            old_text = _card_text(old, contact)
            try:
                await bot.edit_message_text(
                    chat_id=connection["owner_id"],
                    message_id=old["card_message_id"],
                    text=old_text + "\n\n⏭ Заменён новым черновиком",
                    reply_markup=None,
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", old["id"], exc)

    for draft in db.get_drafts_by_status(conn, "approved"):
        if not db.claim_draft(conn, draft["id"]):
            continue  # уже забрано другим процессом/итерацией
        if len(draft["text"]) > 4096:
            res = {"ok": False, "error": "текст длиннее 4096 символов — Telegram не примет"}
        else:
            res = await send_business_reply(bot, conn, draft["chat_id"], draft["text"])
        if res["ok"]:
            db.set_draft_status(conn, draft["id"], "sent")
        else:
            db.set_draft_status(conn, draft["id"], "failed", res["error"])

        if draft["card_message_id"] and connection is not None:
            hist = db.get_history(conn, draft["chat_id"], limit=1)
            contact = hist[0]["sender_name"] if hist else str(draft["chat_id"])
            base_text = _card_text(draft, contact)
            suffix = "\n\n✅ Отправлено" if res["ok"] else f"\n\n⚠️ Не удалось отправить: {res['error']}"
            try:
                await bot.edit_message_text(
                    chat_id=connection["owner_id"],
                    message_id=draft["card_message_id"],
                    text=base_text + suffix,
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft["id"], exc)


async def watch_drafts(
    bot: Bot, conn: sqlite3.Connection, settings: Settings, interval: float = 3.0,
) -> None:
    while True:
        try:
            await process_new_drafts(bot, conn, settings)
        except Exception:  # noqa: BLE001 - цикл не должен умирать
            log.exception("draft watcher iteration failed")
        await asyncio.sleep(interval)


@router.callback_query(F.data.startswith("draft:"))
async def on_draft_callback(cb: CallbackQuery, conn: sqlite3.Connection, bot: Bot) -> None:
    # Validate callback data format before any DB operations
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return

    _, draft_id_s, action = parts

    # Validate draft_id is all digits
    if not draft_id_s.isdigit():
        await cb.answer()
        return

    # Validate action is in allowed set
    if action not in {"approve", "reject"}:
        await cb.answer()
        return

    draft_id = int(draft_id_s)
    draft = db.get_draft(conn, draft_id)
    if draft is not None and draft["status"] == "superseded":
        await cb.answer("Черновик уже неактуален")
        return
    if draft is None or draft["status"] != "awaiting":
        await cb.answer("Черновик уже обработан")
        return
    if action == "approve":
        # Порядок обязателен: карточку правим в ⏳ ДО флипа в 'approved'. Как только статус
        # станет 'approved', вотчер может успеть отправить черновик и поставить финальное
        # «✅ Отправлено» — запоздавшая правка ⏳ перекрыла бы его. Гвардированный флип
        # (WHERE status='awaiting') закрывает TOCTOU с supersede_awaiting: если флип не
        # удался, черновик уже заменён/обработан — возвращаем карточке актуальное состояние.
        try:
            await cb.message.edit_text(text=cb.message.text + "\n\n⏳ Отправляю…", reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "approved"):
            await cb.answer("Черновик уже неактуален")
            try:
                await cb.message.edit_text(
                    text=cb.message.text + "\n\n⏭ Черновик уже неактуален", reply_markup=None
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
            return
        await cb.answer("Отправляю")
    else:
        # Reject: вотчер никогда не трогает 'rejected', гонки с ним нет — флип первым.
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "rejected"):
            await cb.answer("Черновик уже неактуален")
            return
        await cb.answer("Отклонено")
        try:
            await cb.message.edit_text(text=cb.message.text + "\n\n❌ Отклонено", reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
