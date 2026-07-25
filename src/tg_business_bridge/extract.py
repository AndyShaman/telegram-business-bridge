from aiogram.types import Message

_MEDIA_FIELDS = ["photo", "voice", "video", "video_note", "animation", "audio", "document", "sticker"]


def extract_message_row(msg: Message, bot_id: int) -> dict:
    sender = msg.from_user
    if msg.sender_business_bot is not None and msg.sender_business_bot.id == bot_id:
        direction = "out"
    else:
        direction = "in" if sender is not None and sender.id == msg.chat.id else "out"

    media_type = None
    file_id = file_unique_id = None
    file_size = None
    for field in _MEDIA_FIELDS:
        obj = getattr(msg, field, None)
        if not obj:
            continue
        media_type = field
        if field == "photo":
            obj = obj[-1]  # самый крупный вариант
        file_id = obj.file_id
        file_unique_id = obj.file_unique_id
        file_size = getattr(obj, "file_size", None)
        break
    text = msg.text or msg.caption
    if media_type is None:
        media_type = "text" if text is not None else "other"

    sender_name = None
    if sender is not None:
        sender_name = " ".join(filter(None, [sender.first_name, sender.last_name])) or sender.username

    return {
        "connection_id": msg.business_connection_id,
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "sender_id": sender.id if sender else None,
        "sender_name": sender_name,
        "ts": int(msg.date.timestamp()),
        "direction": direction,
        "is_auto": 1 if msg.is_from_offline else 0,
        "media_type": media_type,
        "text": text,
        "media_path": None,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "file_size": file_size,
        # exclude_defaults: aiogram подставляет sentinel Default в незаполненные поля
        # (напр. LinkPreviewOptions) — без него сериализация падает и сообщение теряется
        "raw_json": msg.model_dump_json(exclude_none=True, exclude_defaults=True),
    }


def is_bot_outgoing(msg: Message, bot_id: int) -> bool:
    return msg.sender_business_bot is not None and msg.sender_business_bot.id == bot_id
