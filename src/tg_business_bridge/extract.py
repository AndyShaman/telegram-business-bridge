from aiogram.types import Message

# live_photo первым: у живого фото внутри есть и обычный photo, тип важнее
_MEDIA_FIELDS = [
    "live_photo", "photo", "voice", "video", "video_note", "animation", "audio", "document", "sticker",
]


def _special_text(msg: Message) -> tuple[str, str | None] | None:
    """(тип, текст для поиска) для сообщений без file_id и без text/caption."""
    if msg.poll is not None:
        options = "\n".join(f"- {o.text}" for o in msg.poll.options)
        return "poll", f"{msg.poll.question}\n{options}"
    if msg.checklist is not None:
        tasks = "\n".join(
            f"[{'x' if t.completion_date or t.completed_by_user or t.completed_by_chat else ' '}] {t.text}"
            for t in msg.checklist.tasks
        )
        return "checklist", f"{msg.checklist.title}\n{tasks}"
    if msg.paid_media is not None:
        return "paid_media", None  # текст — подпись, она в caption
    if msg.contact is not None:
        c = msg.contact
        return "contact", " ".join(filter(None, [c.first_name, c.last_name, c.phone_number]))
    if msg.venue is not None:
        return "venue", f"{msg.venue.title}, {msg.venue.address}"
    if msg.location is not None:
        return "location", None
    return None


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
        special = _special_text(msg)
        if special is not None:
            media_type, special_text = special
            text = text or special_text
        else:
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
