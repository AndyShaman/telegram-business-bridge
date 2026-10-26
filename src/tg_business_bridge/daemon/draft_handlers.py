import asyncio
import html
import json
import logging
import re
import sqlite3
import time
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from tg_business_bridge import db, media
from tg_business_bridge.config import Settings
from tg_business_bridge.formatting import HTML, to_html, visible_text
from tg_business_bridge.sender import send_business_reply

log = logging.getLogger(__name__)
router = Router()


def _card_markup(draft_id: int, settings: Settings) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="✅ Отправить", callback_data=f"draft:{draft_id}:approve")]
    if settings.editor_url:
        buttons.append(
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"draft:{draft_id}:edit")
        )
    pause = [InlineKeyboardButton(text="🔕 Пауза", callback_data=f"draft:{draft_id}:pause")]
    return InlineKeyboardMarkup(inline_keyboard=[buttons, pause])


_CARD_LIMIT = 3500
_TRUNCATE_MARKER = "… [обрезано, полный текст будет отправлен]"


_CUT_ENTITY_RE = re.compile(r"&[a-z]*$")


def _attachments_line(attachments: list[dict]) -> str:
    """«📎 2 вложения: photo.jpg (1.2 MB), contract.pdf (340 KB)» — обычный текст."""
    n = len(attachments)
    word = "вложение" if n == 1 else "вложения" if 2 <= n <= 4 else "вложений"  # n <= 10
    parts = []
    for a in attachments:
        name = a["filename"] or a["kind"]
        if len(name) > 64:
            name = name[:63] + "…"
        size = media.human_size(a["file_size"])
        parts.append(f"{name} ({size})" if size else name)
    return f"📎 {n} {word}: " + ", ".join(parts)


def _card_text(draft: sqlite3.Row, contact: str, attachments: list[dict] | None = None) -> str:
    """Карточка владельцу — всегда Telegram HTML (отправлять её нужно с parse_mode='HTML')."""
    # имя контакта приходит из Telegram и не ограничено схемой БД —
    # без обрезки длинное имя съедает лимит и карточка может превысить 4096
    if len(contact) > 64:
        contact = contact[:63] + "…"
    safe_contact = html.escape(contact, quote=False)
    header = f"Черновик ответа для {safe_contact} (chat {draft['chat_id']}):\n\n"
    body = to_html(draft["text"], draft["parse_mode"])
    footer = ""
    if attachments:
        footer = "\n\n" + html.escape(_attachments_line(attachments), quote=False)
    if len(header) + len(body) + len(footer) <= _CARD_LIMIT:
        return header + body + footer
    # HTML нельзя резать посреди тега: длинная карточка теряет форматирование, но не ломается
    limit = max(0, _CARD_LIMIT - len(header) - len(footer) - len(_TRUNCATE_MARKER))
    plain = html.escape(visible_text(draft["text"], draft["parse_mode"]), quote=False)[:limit]
    return header + _CUT_ENTITY_RE.sub("", plain) + _TRUNCATE_MARKER + footer


# До этого момента (time.monotonic) карточки не отправляются: Telegram попросил
# подождать, преждевременные повторы только продлевают flood-лимит
_flood_wait_until = 0.0


def _contact_name(conn: sqlite3.Connection, chat_id: int) -> str:
    return db.last_incoming_sender_name(conn, chat_id) or str(chat_id)


async def _send_card(
    bot: Bot, conn: sqlite3.Connection, connection: sqlite3.Row, draft: sqlite3.Row,
    settings: Settings,
) -> None:
    contact = _contact_name(conn, draft["chat_id"])
    text = _card_text(draft, contact, db.get_draft_attachments(conn, draft["id"]))
    sent = await bot.send_message(
        chat_id=connection["owner_id"], text=text, parse_mode=HTML,
        reply_markup=_card_markup(draft["id"], settings),
    )
    db.set_draft_card(conn, draft["id"], sent.message_id)
    db.set_draft_status(conn, draft["id"], "awaiting")

    for old in db.supersede_awaiting(conn, draft["chat_id"], draft["id"]):
        media.remove_outbox(settings.data_dir, old["id"])
        if old["card_message_id"] is None:
            continue
        old_text = _card_text(old, contact, db.get_draft_attachments(conn, old["id"]))
        try:
            await bot.edit_message_text(
                chat_id=connection["owner_id"],
                message_id=old["card_message_id"],
                text=old_text + "\n\n⏭ Заменён новым черновиком",
                parse_mode=HTML, reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", old["id"], exc)


async def process_new_drafts(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> None:
    global _flood_wait_until
    connection = db.get_enabled_connection(conn)
    if connection is None:
        # Подключения (ещё) нет — черновики ждут, не помечаясь failed: bootstrap
        # в business_handlers подтянет connection при первом же входящем сообщении
        if db.get_drafts_by_status(conn, "pending") or db.get_drafts_by_status(conn, "approved"):
            log.warning("нет активного business connection — черновики ждут")
        return

    if time.monotonic() >= _flood_wait_until:
        for draft in db.get_drafts_by_status(conn, "pending"):
            try:
                await _send_card(bot, conn, connection, draft, settings)
            except TelegramRetryAfter as exc:
                _flood_wait_until = time.monotonic() + exc.retry_after
                log.warning(
                    "flood wait %s сек при отправке карточки черновика %s",
                    exc.retry_after, draft["id"],
                )
                break
            except TelegramBadRequest as exc:
                # чаще всего это битая разметка HTML-черновика: повторы её не исправят,
                # поэтому черновик закрывается с ошибкой — она видна агенту в list_drafts
                log.warning("Telegram отклонил карточку черновика %s: %s", draft["id"], exc)
                db.set_draft_status(
                    conn, draft["id"], "failed", f"Telegram отклонил карточку: {exc}"
                )
                media.remove_outbox(settings.data_dir, draft["id"])
            except Exception:  # noqa: BLE001 - сбойная карточка не должна блокировать остальные
                log.exception("не удалось отправить карточку черновика %s", draft["id"])

    for draft in db.get_drafts_by_status(conn, "approved"):
        if not db.claim_draft(conn, draft["id"]):
            continue  # уже забрано другим процессом/итерацией
        attachments = db.get_draft_attachments(conn, draft["id"])
        if len(visible_text(draft["text"], draft["parse_mode"])) > 4096:
            res = {"ok": False, "error": "текст длиннее 4096 символов — Telegram не примет"}
        else:
            try:
                res = await send_business_reply(
                    bot, conn, draft["chat_id"], draft["text"], draft["parse_mode"],
                    _input_files(settings, attachments),
                )
            except Exception as exc:  # noqa: BLE001 - черновик не должен зависнуть в 'sending'
                log.exception("непредвиденная ошибка отправки черновика %s", draft["id"])
                res = {"ok": False, "error": f"непредвиденная ошибка: {exc}"}
        if res["ok"]:
            db.set_draft_status(conn, draft["id"], "sent")
        else:
            db.set_draft_status(conn, draft["id"], "failed", res["error"])
        media.remove_outbox(settings.data_dir, draft["id"])

        if draft["card_message_id"]:
            base_text = _card_text(draft, _contact_name(conn, draft["chat_id"]), attachments)
            if res["ok"]:
                suffix = "\n\n✅ Отправлено"
            else:
                # текст ошибки приходит из Telegram и может содержать '<' — карточка идёт как HTML
                suffix = f"\n\n⚠️ Не удалось отправить: {html.escape(res['error'], quote=False)}"
            try:
                await bot.edit_message_text(
                    chat_id=connection["owner_id"],
                    message_id=draft["card_message_id"],
                    text=base_text + suffix,
                    parse_mode=HTML,
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft["id"], exc)


def _input_files(settings: Settings, attachments: list[dict]) -> list[dict]:
    """Вложения черновика в виде для отправки: из архива — file_id (Telegram не качает
    файл заново), загруженные агентом — файл из outbox под исходным именем."""
    return [
        {"kind": a["kind"],
         "file": a["file_id"] or FSInputFile(settings.data_dir / a["path"], filename=a["filename"])}
        for a in attachments
    ]


async def watch_drafts(
    bot: Bot, conn: sqlite3.Connection, settings: Settings, interval: float = 3.0,
) -> None:
    while True:
        try:
            await process_new_drafts(bot, conn, settings)
        except Exception:  # noqa: BLE001 - цикл не должен умирать
            log.exception("draft watcher iteration failed")
        await asyncio.sleep(interval)


async def _drop_editor_keyboard(bot: Bot, chat_id: int) -> None:
    """Reply-клавиатуру снимает только сообщение с ReplyKeyboardRemove — отправляем
    минимальное и сразу удаляем, чтобы в диалоге не осталось служебной строки."""
    try:
        stub = await bot.send_message(chat_id=chat_id, text="✂️", reply_markup=ReplyKeyboardRemove())
        await bot.delete_message(chat_id=chat_id, message_id=stub.message_id)
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось убрать клавиатуру редактора: %s", exc)


async def _forget_edit_prompt(
    bot: Bot, conn: sqlite3.Connection, chat_id: int, draft: sqlite3.Row,
) -> None:
    """Удаляет приглашение открыть редактор и забывает его id."""
    if draft["edit_prompt_message_id"] is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=draft["edit_prompt_message_id"])
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось удалить приглашение черновика %s: %s", draft["id"], exc)
    db.set_edit_prompt(conn, draft["id"], None)


async def _cleanup_editor_ui(
    bot: Bot, conn: sqlite3.Connection, chat_id: int, draft: sqlite3.Row,
) -> None:
    """Убирает следы редактора, когда судьба черновика решена мимо него (отправка,
    отклонение). Без записанного приглашения убирать нечего: редактор не открывали."""
    if draft["edit_prompt_message_id"] is None:
        return
    await _forget_edit_prompt(bot, conn, chat_id, draft)
    await _drop_editor_keyboard(bot, chat_id)


@router.callback_query(F.data.startswith("draft:"))
async def on_draft_callback(
    cb: CallbackQuery, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
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

    # Validate action is in allowed set ('reject' — с карточек, разосланных до появления
    # кнопки «Редактировать»: они должны продолжать работать)
    if action not in {"approve", "reject", "edit", "pause"}:
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
    if action == "edit":
        if not settings.editor_url:
            # карточка могла быть разослана до того, как редактор выключили
            await cb.answer("Редактор недоступен")
            return
        # sendData умеет только web_app-кнопка reply-клавиатуры, поэтому редактор
        # открывается вторым шагом. Статус не трогаем: черновик остаётся 'awaiting',
        # владелец может передумать и отправить его с карточки как есть.
        editor_html = to_html(draft["text"], draft["parse_mode"])
        url = f"{settings.editor_url}#id={draft_id}&html={quote(editor_html)}"
        if draft["edit_prompt_message_id"] is not None:
            # повторные нажатия «Редактировать» не должны копить приглашения в диалоге
            try:
                await bot.delete_message(
                    chat_id=cb.message.chat.id, message_id=draft["edit_prompt_message_id"]
                )
            except Exception as exc:  # noqa: BLE001 - уборка чата не критична
                log.warning("не удалось удалить приглашение черновика %s: %s", draft_id, exc)
        try:
            sent = await bot.send_message(
                chat_id=cb.message.chat.id,
                text=f"✏️ Редактирование черновика №{draft_id} — открой редактор кнопкой ниже",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[
                        KeyboardButton(text="✏️ Открыть редактор", web_app=WebAppInfo(url=url))
                    ]],
                    resize_keyboard=True, one_time_keyboard=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - без ответа на callback юзер видит вечный спиннер
            # длинный черновик после urlencode раздувает URL кнопки — Telegram может его не принять
            log.warning("не удалось открыть редактор черновика %s: %s", draft_id, exc)
            await cb.answer(
                "Не удалось открыть редактор — черновик слишком длинный или произошла ошибка"
            )
            return
        db.set_edit_prompt(conn, draft_id, sent.message_id)
        await cb.answer()
    elif action == "approve":
        # Порядок обязателен: карточку правим в ⏳ ДО флипа в 'approved'. Как только статус
        # станет 'approved', вотчер может успеть отправить черновик и поставить финальное
        # «✅ Отправлено» — запоздавшая правка ⏳ перекрыла бы его. Гвардированный флип
        # (WHERE status='awaiting') закрывает TOCTOU с supersede_awaiting: если флип не
        # удался, черновик уже заменён/обработан — возвращаем карточке актуальное состояние.
        try:
            # html_text, а не text: иначе правка карточки теряет entities (ссылки, жирный)
            await cb.message.edit_text(
                text=cb.message.html_text + "\n\n⏳ Отправляю…",
                parse_mode=HTML, reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "approved"):
            await cb.answer("Черновик уже неактуален")
            try:
                await cb.message.edit_text(
                    text=cb.message.html_text + "\n\n⏭ Черновик уже неактуален",
                    parse_mode=HTML, reply_markup=None,
                )
            except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
                log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
            return
        await cb.answer("Отправляю")
        await _cleanup_editor_ui(bot, conn, cb.message.chat.id, draft)
    else:
        # Reject и пауза: вотчер никогда не трогает 'rejected', гонки с ним нет — флип первым.
        if not db.set_draft_status_if(conn, draft_id, "awaiting", "rejected"):
            await cb.answer("Черновик уже неактуален")
            return
        media.remove_outbox(settings.data_dir, draft_id)
        if action == "pause":
            db.pause_chat(conn, draft["chat_id"])
            await cb.answer("Чат на паузе")
            mark = "🔕 Чат на паузе: черновики агента для него не принимаются. Снять — /paused"
        else:
            await cb.answer("Отклонено")
            mark = "❌ Отклонено"
        try:
            await cb.message.edit_text(
                text=cb.message.html_text + "\n\n" + mark, parse_mode=HTML, reply_markup=None
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft_id, exc)
        await _cleanup_editor_ui(bot, conn, cb.message.chat.id, draft)


def _parse_editor_payload(raw: str) -> tuple[int, str, str | None] | None:
    """(draft_id, text, parse_mode) из данных мини-приложения или None, если пришёл мусор.
    Поле html — текущий редактор, text — страница старой версии (её могли разместить
    самостоятельно). Сам текст никогда не попадает в логи — это личная переписка."""
    try:
        payload = json.loads(raw)
        draft_id_s = str(payload["id"])
        if "html" in payload:
            text, parse_mode = payload["html"], HTML
        else:
            text, parse_mode = payload["text"], None
    except (ValueError, TypeError, KeyError):
        return None
    if not draft_id_s.isdigit() or not isinstance(text, str):
        return None
    # и пустота, и лимит считаются по видимому тексту: теги в счёт не идут
    shown = visible_text(text, parse_mode)
    if not shown.strip() or len(shown) > 4096:  # Telegram не примет такой ответ при отправке
        return None
    return int(draft_id_s), text, parse_mode


async def _repost_card(
    bot: Bot, conn: sqlite3.Connection, owner_id: int, draft: sqlite3.Row, settings: Settings,
) -> None:
    """Пересоздаёт карточку внизу диалога — после правки она должна быть последней.
    Статус не трогает: _send_card здесь не подходит, он ставит 'awaiting' и делает
    supersede, а черновик к этому моменту мог быть уже одобрен.

    Сначала новая карточка, только потом уборка старой: если отправка сорвётся,
    у владельца останется рабочая старая карточка, а не черновик вообще без кнопок."""
    text = _card_text(
        draft, _contact_name(conn, draft["chat_id"]), db.get_draft_attachments(conn, draft["id"])
    )
    old_card_id = draft["card_message_id"]
    sent = await bot.send_message(
        chat_id=owner_id, text=text, parse_mode=HTML,
        reply_markup=_card_markup(draft["id"], settings),
    )
    db.set_draft_card(conn, draft["id"], sent.message_id)
    if old_card_id is None:
        return
    try:
        await bot.delete_message(chat_id=owner_id, message_id=old_card_id)
    except Exception as exc:  # noqa: BLE001 - не удалилась, так хотя бы гасим кнопки
        log.warning("не удалось удалить карточку черновика %s: %s", draft["id"], exc)
        try:
            await bot.edit_message_text(
                chat_id=owner_id, message_id=old_card_id,
                text=text + "\n\n⏭ Заменён обновлённой карточкой",
                parse_mode=HTML, reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - редактирование карточки не критично
            log.warning("не удалось отредактировать карточку черновика %s: %s", draft["id"], exc)


@router.message(F.web_app_data)
async def on_editor_result(
    msg: Message, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
    connection = db.get_enabled_connection(conn)
    if connection is None or msg.from_user is None:
        return
    owner_id = connection["owner_id"]
    if msg.from_user.id != owner_id:
        # правки чужих аккаунтов молча игнорируем: черновики видит только владелец
        log.warning("web_app_data от постороннего пользователя %s — пропущено", msg.from_user.id)
        return

    # плашка «вы передали данные боту» — служебный шум, убираем при любом исходе
    try:
        await msg.delete()
    except Exception as exc:  # noqa: BLE001 - уборка чата не критична
        log.warning("не удалось удалить сообщение редактора: %s", exc)

    parsed = _parse_editor_payload(msg.web_app_data.data)
    if parsed is None:
        await msg.answer("Не удалось обработать данные редактора", reply_markup=ReplyKeyboardRemove())
        return
    draft_id, edited_text, parse_mode = parsed

    if not db.update_draft_text(conn, draft_id, edited_text, parse_mode):
        # черновик ушёл из 'awaiting', пока редактор был открыт: остальные пути уборки
        # стоят за тем же гвардом, так что приглашение убрать больше некому
        stale = db.get_draft(conn, draft_id)
        if stale is not None:
            await _forget_edit_prompt(bot, conn, owner_id, stale)
        await msg.answer("Черновик уже неактуален", reply_markup=ReplyKeyboardRemove())
        return

    draft = db.get_draft(conn, draft_id)
    await _forget_edit_prompt(bot, conn, owner_id, draft)
    # клавиатуру снимаем безусловно: раз пришли данные редактора, она точно на экране,
    # а id приглашения может быть пуст у черновиков, застрявших в правке при обновлении
    await _drop_editor_keyboard(bot, owner_id)
    # отдельного «черновик обновлён» нет: подтверждение — сама свежая карточка внизу
    await _repost_card(bot, conn, owner_id, draft, settings)


def _paused_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup | None]:
    """Список чатов на паузе с кнопкой снятия под каждым — обычный текст, без разметки."""
    chats = db.list_paused_chats(conn)
    if not chats:
        return "Чатов на паузе нет.", None
    lines = ["Чаты на паузе — черновики агента для них не принимаются:"]
    rows = []
    for c in chats:
        name = c["sender_name"] or str(c["chat_id"])
        if len(name) > 64:
            name = name[:63] + "…"
        username = f" (@{c['username']})" if isinstance(c["username"], str) else ""
        lines.append(f"• {name}{username}, chat {c['chat_id']}")
        rows.append([InlineKeyboardButton(
            text=f"▶️ Снять паузу: {name[:32]}", callback_data=f"unpause:{c['chat_id']}"
        )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _is_owner(conn: sqlite3.Connection, user) -> bool:
    connection = db.get_enabled_connection(conn)
    return connection is not None and user is not None and user.id == connection["owner_id"]


@router.message(Command("paused"))
async def on_paused_command(msg: Message, conn: sqlite3.Connection) -> None:
    if not _is_owner(conn, msg.from_user):
        return  # список чатов — личные данные владельца
    text, markup = _paused_view(conn)
    await msg.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("unpause:"))
async def on_unpause_callback(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    chat_id_s = cb.data.split(":", 1)[1]
    if not _is_owner(conn, cb.from_user) or not chat_id_s.lstrip("-").isdigit():
        await cb.answer()
        return
    db.unpause_chat(conn, int(chat_id_s))
    await cb.answer("Пауза снята")
    text, markup = _paused_view(conn)
    try:
        await cb.message.edit_text(text=text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 - редактирование списка не критично
        log.warning("не удалось обновить список чатов на паузе: %s", exc)
