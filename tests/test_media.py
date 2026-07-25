import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tg_business_bridge import db
from tg_business_bridge.config import Settings
from tg_business_bridge.media import MAX_DOWNLOAD_BYTES, cleanup_expired_media, download_media, should_download


def test_should_download_respects_limit():
    assert should_download({"file_id": "F", "file_size": 100}) is True
    assert should_download({"file_id": "F", "file_size": None}) is True
    assert should_download({"file_id": "F", "file_size": MAX_DOWNLOAD_BYTES + 1}) is False
    assert should_download({"file_id": None, "file_size": None}) is False


@pytest.mark.asyncio
async def test_download_media_saves_file(tmp_path):
    bot = AsyncMock()
    bot.get_file.return_value.file_path = "voice/file_1.oga"

    async def fake_download(file_path, destination):
        Path(destination).write_bytes(b"data")

    bot.download_file.side_effect = fake_download
    row = {"file_id": "F1", "file_unique_id": "U1", "file_size": 10, "ts": 1751800000}
    rel = await download_media(bot, row, tmp_path / "media")
    assert rel is not None
    saved = tmp_path / rel
    assert saved.exists() and saved.read_bytes() == b"data"
    assert saved.name == "U1.oga"


@pytest.mark.asyncio
async def test_download_media_swallows_api_error(tmp_path):
    from aiogram.exceptions import TelegramBadRequest

    bot = AsyncMock()
    bot.get_file.side_effect = TelegramBadRequest(method=AsyncMock(), message="file is too big")
    row = {"file_id": "F1", "file_unique_id": "U1", "file_size": None, "ts": 1751800000}
    assert await download_media(bot, row, tmp_path / "media") is None


@pytest.mark.asyncio
async def test_download_media_cleans_partial_file_on_failure(tmp_path):
    from aiogram.exceptions import TelegramNetworkError

    bot = AsyncMock()
    bot.get_file.return_value.file_path = "voice/file_1.oga"

    async def broken_download(file_path, destination):
        Path(destination).write_bytes(b"part")  # успело записаться до обрыва
        raise TelegramNetworkError(method=AsyncMock(), message="connection reset")

    bot.download_file.side_effect = broken_download
    row = {"file_id": "F1", "file_unique_id": "U1", "file_size": 10, "ts": 1751800000}
    assert await download_media(bot, row, tmp_path / "media") is None
    leftovers = list((tmp_path / "media").rglob("*")) if (tmp_path / "media").exists() else []
    assert not [p for p in leftovers if p.is_file()]  # ни готового, ни .part файла


@pytest.mark.asyncio
async def test_download_media_returns_none_if_file_path_is_none(tmp_path):
    bot = AsyncMock()
    bot.get_file.return_value.file_path = None
    row = {"file_id": "F1", "file_unique_id": "U1", "file_size": 10, "ts": 1751800000}
    result = await download_media(bot, row, tmp_path / "media")
    assert result is None
    assert not (tmp_path / "media").exists()


def _settings(tmp_path, monkeypatch, retention_days=0):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_MEDIA_RETENTION_DAYS", str(retention_days))
    return Settings()


def _msg(**over):
    row = dict(
        connection_id="conn1", chat_id=777, message_id=1, sender_id=777,
        sender_name="Alice", ts=1000, direction="in", is_auto=0,
        media_type="photo", text=None, raw_json="{}",
    )
    row.update(over)
    return row


def _add_media_row(conn, tmp_path, rel_path, ts, message_id):
    rowid = db.insert_message(conn, _msg(message_id=message_id, ts=ts, file_id=f"F{message_id}"))
    db.set_media_path(conn, rowid, rel_path)
    abs_path = tmp_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"data")
    return rowid


def test_cleanup_expired_media_noop_when_retention_disabled(conn, tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, retention_days=0)
    old_ts = int(time.time()) - 100 * 86400
    rowid = _add_media_row(conn, tmp_path, "media/old.jpg", old_ts, message_id=1)

    assert cleanup_expired_media(conn, settings) == 0
    assert (tmp_path / "media/old.jpg").exists()
    got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["media_path"] == "media/old.jpg"


def test_cleanup_expired_media_deletes_old_keeps_fresh(conn, tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, retention_days=7)
    now = int(time.time())
    old_id = _add_media_row(conn, tmp_path, "media/old.jpg", now - 10 * 86400, message_id=1)
    fresh_id = _add_media_row(conn, tmp_path, "media/fresh.jpg", now - 1 * 86400, message_id=2)

    assert cleanup_expired_media(conn, settings) == 1

    assert not (tmp_path / "media/old.jpg").exists()
    old_row = conn.execute("SELECT media_path FROM messages WHERE id=?", (old_id,)).fetchone()
    assert old_row["media_path"] is None

    assert (tmp_path / "media/fresh.jpg").exists()
    fresh_row = conn.execute("SELECT media_path FROM messages WHERE id=?", (fresh_id,)).fetchone()
    assert fresh_row["media_path"] == "media/fresh.jpg"


def test_cleanup_expired_media_keeps_file_shared_with_fresh_row(conn, tmp_path, monkeypatch):
    # file_unique_id стабилен: одно и то же медиа могут прислать дважды, и обе строки
    # ссылаются на один файл. Пока на файл ссылается свежая строка — не удаляем его
    # и не трогаем media_path устаревшей строки (файл жив, ссылка валидна).
    settings = _settings(tmp_path, monkeypatch, retention_days=7)
    now = int(time.time())
    old_id = _add_media_row(conn, tmp_path, "media/shared.jpg", now - 10 * 86400, message_id=1)
    fresh_id = _add_media_row(conn, tmp_path, "media/shared.jpg", now - 1 * 86400, message_id=2)

    assert cleanup_expired_media(conn, settings) == 0
    assert (tmp_path / "media/shared.jpg").exists()
    for rowid in (old_id, fresh_id):
        got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
        assert got["media_path"] == "media/shared.jpg"


def test_cleanup_expired_media_shared_path_both_expired(conn, tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, retention_days=7)
    now = int(time.time())
    id_a = _add_media_row(conn, tmp_path, "media/shared.jpg", now - 10 * 86400, message_id=1)
    id_b = _add_media_row(conn, tmp_path, "media/shared.jpg", now - 9 * 86400, message_id=2)

    assert cleanup_expired_media(conn, settings) == 2
    assert not (tmp_path / "media/shared.jpg").exists()
    for rowid in (id_a, id_b):
        got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
        assert got["media_path"] is None


def test_cleanup_expired_media_missing_file_still_clears_path(conn, tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, retention_days=7)
    old_ts = int(time.time()) - 10 * 86400
    rowid = db.insert_message(conn, _msg(message_id=1, ts=old_ts, file_id="F1"))
    db.set_media_path(conn, rowid, "media/gone.jpg")  # файла на диске нет

    assert cleanup_expired_media(conn, settings) == 1
    got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["media_path"] is None


def test_cleanup_expired_media_skips_path_escape(conn, tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path, monkeypatch, retention_days=7)
    old_ts = int(time.time()) - 10 * 86400
    rowid = db.insert_message(conn, _msg(message_id=1, ts=old_ts, file_id="F1"))
    db.set_media_path(conn, rowid, "../../etc/passwd")

    with caplog.at_level("WARNING"):
        assert cleanup_expired_media(conn, settings) == 0

    got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["media_path"] == "../../etc/passwd"
    assert any("data_dir" in r.message for r in caplog.records)
