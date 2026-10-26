import asyncio
import json
import logging
import sqlite3

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

from tg_business_bridge import db
from tg_business_bridge.extract import extract_message_row
from tg_business_bridge.formatting import visible_text

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # лимит подписи к медиа в Telegram, по видимому тексту
_ALBUM_MEDIA = {
    "photo": InputMediaPhoto, "video": InputMediaVideo,
    "document": InputMediaDocument, "audio": InputMediaAudio,
}


async def _call(method, **kwargs):
    """Один повтор после flood-ответа Telegram; повторный RetryAfter уходит наверх."""
    try:
        return await method(**kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await method(**kwargs)


async def send_business_reply(
    bot, conn: sqlite3.Connection, chat_id: int, text: str, parse_mode: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """attachments — [{"kind": photo|video|document|audio|voice, "file": file_id | InputFile}].
    Одно вложение — send_<kind>, 2-10 — альбом. Текст идёт подписью, если влезает в
    CAPTION_LIMIT, иначе отдельным сообщением перед медиа."""
    connection = db.get_enabled_connection(conn)
    if connection is None:
        return {"ok": False, "error": "no active business connection"}
    rights = json.loads(connection["rights_json"] or "{}")
    if not rights.get("can_reply"):
        return {"ok": False, "error": "bot lacks can_reply right"}

    cid = connection["connection_id"]
    base = {"chat_id": chat_id, "business_connection_id": cid}

    def store(sent) -> None:
        # Telegram не присылает эхо собственных business-сообщений через getUpdates,
        # поэтому сохраняем отправленное сообщение сразу; сбой сохранения не должен
        # превращать успешную отправку в ошибку.
        try:
            db.insert_message(conn, extract_message_row(sent, bot_id=bot.id))
        except Exception as e:  # noqa: BLE001 - best-effort
            log.warning("failed to store sent message: %s", e)

    try:
        if not attachments:
            store(await _call(bot.send_message, text=text, parse_mode=parse_mode, **base))
        else:
            shown = visible_text(text, parse_mode)
            fits = len(shown) <= CAPTION_LIMIT
            if shown.strip() and not fits:
                store(await _call(bot.send_message, text=text, parse_mode=parse_mode, **base))
            cap = {"caption": text, "parse_mode": parse_mode} if shown.strip() and fits else {}
            if len(attachments) == 1:
                a = attachments[0]
                method = getattr(bot, f"send_{a['kind']}")
                store(await _call(method, **{a["kind"]: a["file"]}, **cap, **base))
            else:
                media = [
                    _ALBUM_MEDIA[a["kind"]](media=a["file"], **(cap if i == 0 else {}))
                    for i, a in enumerate(attachments)
                ]
                for sent in await _call(bot.send_media_group, media=media, **base):
                    store(sent)
    # Ловим всю иерархию TelegramAPIError (BadRequest, Forbidden, сеть, 5xx, повторный
    # RetryAfter): иначе исключение уйдёт выше и черновик навсегда останется в 'sending'
    except TelegramAPIError as e:
        return {"ok": False, "error": str(e)}

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
