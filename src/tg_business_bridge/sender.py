import asyncio
import json
import logging
import sqlite3

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from tg_business_bridge import db
from tg_business_bridge.extract import extract_message_row

log = logging.getLogger(__name__)


async def send_business_reply(bot, conn: sqlite3.Connection, chat_id: int, text: str) -> dict:
    connection = db.get_enabled_connection(conn)
    if connection is None:
        return {"ok": False, "error": "no active business connection"}
    rights = json.loads(connection["rights_json"] or "{}")
    if not rights.get("can_reply"):
        return {"ok": False, "error": "bot lacks can_reply right"}

    cid = connection["connection_id"]
    try:
        try:
            sent = await bot.send_message(chat_id=chat_id, text=text, business_connection_id=cid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            sent = await bot.send_message(chat_id=chat_id, text=text, business_connection_id=cid)
    # Ловим всю иерархию TelegramAPIError (BadRequest, Forbidden, сеть, 5xx, повторный
    # RetryAfter): иначе исключение уйдёт выше и черновик навсегда останется в 'sending'
    except TelegramAPIError as e:
        return {"ok": False, "error": str(e)}

    # Telegram не присылает эхо собственных business-сообщений через getUpdates,
    # поэтому сохраняем отправленное сообщение сразу; сбой сохранения не должен
    # превращать успешную отправку в ошибку.
    try:
        db.insert_message(conn, extract_message_row(sent, bot_id=bot.id))
    except Exception as e:  # noqa: BLE001 - best-effort
        log.warning("failed to store sent message: %s", e)

    if rights.get("can_read_messages"):
        mid = db.last_incoming_message_id(conn, chat_id)
        if mid is not None:
            try:
                await bot.read_business_message(
                    business_connection_id=cid, chat_id=chat_id, message_id=mid
                )
            except Exception as e:  # noqa: BLE001 - best-effort
                log.warning("read receipt failed: %s", e)
    return {"ok": True, "error": None}
