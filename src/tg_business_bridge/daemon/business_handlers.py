import logging
import sqlite3
import time

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from tg_business_bridge import db, media, transcribe
from tg_business_bridge.config import Settings
from tg_business_bridge.extract import extract_message_row

log = logging.getLogger(__name__)
router = Router()

_TRANSCRIBABLE_MEDIA = {"voice", "audio", "video_note"}
_BOOTSTRAP_RETRY_SEC = 300
_bootstrap_failed_at: dict[str, float] = {}


@router.business_connection()
async def on_business_connection(event: BusinessConnection, conn: sqlite3.Connection) -> None:
    rights_json = event.rights.model_dump_json(exclude_none=True) if event.rights else "{}"
    db.upsert_connection(conn, event.id, event.user.id, rights_json, event.is_enabled)
    log.info("business connection %s enabled=%s", event.id, event.is_enabled)


@router.business_message()
async def on_business_message(
    msg: Message, conn: sqlite3.Connection, bot: Bot, settings: Settings,
) -> None:
    row = extract_message_row(msg, bot_id=bot.id)
    connection_id = row["connection_id"]
    failed_at = _bootstrap_failed_at.get(connection_id)
    if failed_at is None or time.monotonic() - failed_at >= _BOOTSTRAP_RETRY_SEC:
        # business_connection-событие иногда не долетает от Telegram — самостоятельно
        # запрашиваем данные подключения, чтобы черновики не зависали без адресата.
        # Перезапрашиваем и при is_enabled=0: владелец мог переподключить бота, а апдейт
        # о включении не дойти — само входящее сообщение уже значит, что связь живая.
        # Никакой сбой bootstrap не должен помешать сохранению сообщения.
        try:
            existing = db.get_connection(conn, connection_id)
            if existing is None or not existing["is_enabled"]:
                bc = await bot.get_business_connection(connection_id)
                rights_json = bc.rights.model_dump_json(exclude_none=True) if bc.rights else "{}"
                db.upsert_connection(conn, bc.id, bc.user.id, rights_json, bc.is_enabled)
                log.info("bootstrapped business connection %s via getBusinessConnection", bc.id)
            _bootstrap_failed_at.pop(connection_id, None)
        except Exception as exc:
            log.warning("bootstrap подключения %s не удался: %s", connection_id, exc)
            _bootstrap_failed_at[connection_id] = time.monotonic()
    if db.find_message_rowid(conn, row["chat_id"], row["message_id"]) is not None:
        # Telegram передоставляет апдейты после падения демона до подтверждения offset —
        # без проверки одно сообщение появлялось бы в истории и поиске дважды
        log.info("повторная доставка msg %s в чате %s — пропущено", row["message_id"], row["chat_id"])
        return
    rowid = db.insert_message(conn, row)
    if media.should_download(row):
        rel = await media.download_media(bot, row, settings.media_dir)
        if rel is not None:
            db.set_media_path(conn, rowid, rel)
            if settings.deepgram_api_key and row["media_type"] in _TRANSCRIBABLE_MEDIA:
                abs_path = str(settings.data_dir / rel)
                transcript = await transcribe.transcribe_file(abs_path, settings.deepgram_api_key)
                if transcript:
                    text = f"{row['text']}\n{transcript}" if row["text"] else transcript
                    db.set_message_text(conn, rowid, text)


@router.edited_business_message()
async def on_edited_business_message(msg: Message, conn: sqlite3.Connection) -> None:
    new_text = msg.text or msg.caption
    db.add_message_event(
        conn, msg.chat.id, msg.message_id, "edited",
        msg.edit_date or int(time.time()), new_text=new_text,
    )
    if new_text is not None:
        # без этого история, поиск и FTS продолжали бы отдавать доредакционный текст
        rowid = db.find_message_rowid(conn, msg.chat.id, msg.message_id)
        if rowid is not None:
            db.set_message_text(conn, rowid, new_text)


@router.deleted_business_messages()
async def on_deleted_business_messages(
    event: BusinessMessagesDeleted, conn: sqlite3.Connection,
) -> None:
    now = int(time.time())
    for mid in event.message_ids:
        db.add_message_event(conn, event.chat.id, mid, "deleted", now)
