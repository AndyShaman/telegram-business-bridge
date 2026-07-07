import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from tg_business_bridge import db
from tg_business_bridge.config import Settings

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def should_download(row: dict) -> bool:
    if not row.get("file_id"):
        return False
    size = row.get("file_size")
    return size is None or size <= MAX_DOWNLOAD_BYTES


async def download_media(bot: Bot, row: dict, media_dir: Path) -> str | None:
    try:
        file = await bot.get_file(row["file_id"])
        if not file.file_path:
            return None
        ext = Path(file.file_path).suffix if file.file_path else ""
        month = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%Y-%m")
        dest_dir = media_dir / month
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{row['file_unique_id']}{ext}"
        await bot.download_file(file.file_path, destination=str(dest))
        return str(dest.relative_to(media_dir.parent))
    except (TelegramAPIError, OSError) as e:
        log.warning("media download failed for %s: %s", row.get("file_id"), e)
        return None


def cleanup_expired_media(conn: sqlite3.Connection, settings: Settings) -> int:
    """Удаляет с диска файлы медиа старше settings.media_retention_days дней и обнуляет
    media_path в БД. Текст сообщения и raw_json не трогаются — тексты хранятся вечно.
    media_retention_days <= 0 (по умолчанию) — хранить вечно, ничего не делает."""
    if settings.media_retention_days <= 0:
        return 0
    cutoff_ts = int(time.time()) - settings.media_retention_days * 86400
    data_dir = settings.data_dir.resolve()
    count = 0
    for row in db.list_expired_media(conn, cutoff_ts):
        abs_path = (settings.data_dir / row["media_path"]).resolve()
        try:
            abs_path.relative_to(data_dir)
        except ValueError:
            log.warning(
                "media_path вышел за пределы data_dir для сообщения id=%s: %s — пропущено",
                row["id"], row["media_path"],
            )
            continue
        try:
            abs_path.unlink()
        except FileNotFoundError:
            pass  # файла уже нет — всё равно обнуляем media_path
        db.clear_media_path(conn, row["id"])
        count += 1
    if count > 0:
        log.info("media retention: удалено %d файл(ов)", count)
    return count


async def watch_media_cleanup(conn: sqlite3.Connection, settings: Settings, interval: float = 86400) -> None:
    """Раз в сутки прогоняет cleanup_expired_media (первый прогон — сразу при старте)."""
    while True:
        try:
            cleanup_expired_media(conn, settings)
        except Exception:  # noqa: BLE001 - цикл не должен умирать
            log.exception("media cleanup iteration failed")
        await asyncio.sleep(interval)
