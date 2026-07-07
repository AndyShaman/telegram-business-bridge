import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from tg_business_bridge import db
from tg_business_bridge.config import Settings
from tg_business_bridge.daemon import business_handlers
from tg_business_bridge.daemon.business_handlers import (
    on_business_connection,
    on_business_message,
    on_deleted_business_messages,
    on_edited_business_message,
)
from test_extract import _m


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture(autouse=True)
def _clear_bootstrap_cooldown():
    business_handlers._bootstrap_failed_at.clear()
    yield
    business_handlers._bootstrap_failed_at.clear()


def _bot():
    bot = AsyncMock()
    bot.id = 99
    return bot


@pytest.mark.asyncio
async def test_connection_saved(conn):
    event = BusinessConnection.model_validate({
        "id": "c1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": 42,
        "date": 1751800000,
        "is_enabled": True,
        "rights": {"can_reply": True, "can_read_messages": True},
    })
    await on_business_connection(event, conn=conn)
    saved = db.get_enabled_connection(conn)
    assert saved["connection_id"] == "c1" and saved["owner_id"] == 42
    assert '"can_reply":true' in saved["rights_json"].replace(" ", "")


@pytest.mark.asyncio
async def test_text_message_stored(conn, settings):
    await on_business_message(_m(text="привет"), conn=conn, bot=_bot(), settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет" and row["direction"] == "in"


@pytest.mark.asyncio
async def test_media_message_downloads(conn, settings):
    bot = _bot()
    bot.get_file.return_value.file_path = "voice/f.oga"

    async def fake_download(file_path, destination):
        from pathlib import Path
        Path(destination).write_bytes(b"v")

    bot.download_file.side_effect = fake_download
    m = _m(voice={"file_id": "V1", "file_unique_id": "U1", "duration": 2, "file_size": 100})
    await on_business_message(m, conn=conn, bot=bot, settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["media_type"] == "voice" and row["media_path"] is not None


@pytest.mark.asyncio
async def test_voice_message_transcribed_when_api_key_set(conn, settings, monkeypatch):
    monkeypatch.setenv("BRIDGE_DEEPGRAM_API_KEY", "dg-key")
    settings = Settings()
    bot = _bot()
    bot.get_file.return_value.file_path = "voice/f.oga"

    async def fake_download(file_path, destination):
        from pathlib import Path
        Path(destination).write_bytes(b"v")

    bot.download_file.side_effect = fake_download
    m = _m(voice={"file_id": "V1", "file_unique_id": "U1", "duration": 2, "file_size": 100})
    with patch(
        "tg_business_bridge.daemon.business_handlers.transcribe.transcribe_file",
        AsyncMock(return_value="расшифрованный текст"),
    ) as mock_transcribe:
        await on_business_message(m, conn=conn, bot=bot, settings=settings)
    mock_transcribe.assert_awaited_once()
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "расшифрованный текст"


@pytest.mark.asyncio
async def test_voice_message_caption_appends_transcript(conn, settings, monkeypatch):
    monkeypatch.setenv("BRIDGE_DEEPGRAM_API_KEY", "dg-key")
    settings = Settings()
    bot = _bot()
    bot.get_file.return_value.file_path = "voice/f.oga"

    async def fake_download(file_path, destination):
        from pathlib import Path
        Path(destination).write_bytes(b"v")

    bot.download_file.side_effect = fake_download
    m = _m(
        voice={"file_id": "V1", "file_unique_id": "U1", "duration": 2, "file_size": 100},
        caption="подпись",
    )
    with patch(
        "tg_business_bridge.daemon.business_handlers.transcribe.transcribe_file",
        AsyncMock(return_value="расшифрованный текст"),
    ):
        await on_business_message(m, conn=conn, bot=bot, settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "подпись\nрасшифрованный текст"


@pytest.mark.asyncio
async def test_voice_message_not_transcribed_without_api_key(conn, settings):
    bot = _bot()
    bot.get_file.return_value.file_path = "voice/f.oga"

    async def fake_download(file_path, destination):
        from pathlib import Path
        Path(destination).write_bytes(b"v")

    bot.download_file.side_effect = fake_download
    m = _m(voice={"file_id": "V1", "file_unique_id": "U1", "duration": 2, "file_size": 100})
    with patch(
        "tg_business_bridge.daemon.business_handlers.transcribe.transcribe_file",
        AsyncMock(),
    ) as mock_transcribe:
        await on_business_message(m, conn=conn, bot=bot, settings=settings)
    mock_transcribe.assert_not_called()
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] is None


@pytest.mark.asyncio
async def test_oversized_media_keeps_file_id_only(conn, settings):
    bot = _bot()
    m = _m(video={"file_id": "BIG", "file_unique_id": "UB", "width": 1, "height": 1,
                  "duration": 1, "file_size": 21 * 1024 * 1024})
    await on_business_message(m, conn=conn, bot=bot, settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["media_path"] is None and row["file_id"] == "BIG"
    bot.get_file.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_connection_bootstrapped_via_api(conn, settings):
    bot = _bot()
    bot.get_business_connection.return_value = BusinessConnection.model_validate({
        "id": "conn1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": 42,
        "date": 1751800000,
        "is_enabled": True,
        "rights": {"can_reply": True, "can_read_messages": True},
    })
    await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    bot.get_business_connection.assert_awaited_once_with("conn1")
    saved = db.get_connection(conn, "conn1")
    assert saved is not None and saved["owner_id"] == 42 and saved["is_enabled"] == 1
    assert '"can_reply":true' in saved["rights_json"].replace(" ", "")
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет"


@pytest.mark.asyncio
async def test_known_connection_skips_api_call(conn, settings):
    db.upsert_connection(conn, "conn1", 42, "{}", True)
    bot = _bot()
    await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    bot.get_business_connection.assert_not_called()
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет"


@pytest.mark.asyncio
async def test_disabled_connection_rebootstrapped_via_api(conn, settings):
    # В БД лежит устаревшая запись is_enabled=0 (владелец переподключил бота, апдейт
    # о включении не дошёл). Входящее сообщение должно перезапросить подключение,
    # иначе черновики навсегда падают в failed из-за «нет активного подключения».
    db.upsert_connection(conn, "conn1", 42, "{}", False)
    bot = _bot()
    bot.get_business_connection.return_value = BusinessConnection.model_validate({
        "id": "conn1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": 42,
        "date": 1751800000,
        "is_enabled": True,
        "rights": {"can_reply": True, "can_read_messages": True},
    })
    await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    bot.get_business_connection.assert_awaited_once_with("conn1")
    saved = db.get_connection(conn, "conn1")
    assert saved["is_enabled"] == 1


@pytest.mark.asyncio
async def test_bootstrap_api_error_still_stores_message(conn, settings):
    bot = _bot()
    bot.get_business_connection.side_effect = RuntimeError("boom")
    await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    assert db.get_connection(conn, "conn1") is None
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет"


@pytest.mark.asyncio
async def test_get_connection_error_still_stores_message(conn, settings, monkeypatch):
    bot = _bot()
    monkeypatch.setattr(
        db, "get_connection",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет"


@pytest.mark.asyncio
async def test_upsert_connection_error_still_stores_message(conn, settings):
    bot = _bot()
    bot.get_business_connection.return_value = BusinessConnection.model_validate({
        "id": "conn1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": 42,
        "date": 1751800000,
        "is_enabled": False,
        "rights": {"can_reply": True, "can_read_messages": True},
    })
    with patch(
        "tg_business_bridge.daemon.business_handlers.db.upsert_connection",
        side_effect=sqlite3.OperationalError("locked"),
    ):
        await on_business_message(_m(text="привет"), conn=conn, bot=bot, settings=settings)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["text"] == "привет"


@pytest.mark.asyncio
async def test_bootstrap_failure_has_cooldown_before_retry(conn, settings, monkeypatch):
    bot = _bot()
    bot.get_business_connection.side_effect = RuntimeError("boom")

    fake_time = [1000.0]
    monkeypatch.setattr(business_handlers.time, "monotonic", lambda: fake_time[0])

    await on_business_message(_m(text="раз"), conn=conn, bot=bot, settings=settings)
    bot.get_business_connection.assert_awaited_once()

    await on_business_message(_m(text="два"), conn=conn, bot=bot, settings=settings)
    bot.get_business_connection.assert_awaited_once()  # still just once — cooldown active

    fake_time[0] += business_handlers._BOOTSTRAP_RETRY_SEC + 1
    await on_business_message(_m(text="три"), conn=conn, bot=bot, settings=settings)
    assert bot.get_business_connection.await_count == 2  # cooldown expired — retried

    rows = conn.execute("SELECT text FROM messages ORDER BY id").fetchall()
    assert [r["text"] for r in rows] == ["раз", "два", "три"]


@pytest.mark.asyncio
async def test_edited_and_deleted_events(conn):
    await on_edited_business_message(_m(text="новый текст"), conn=conn)
    event = BusinessMessagesDeleted.model_validate({
        "business_connection_id": "c1",
        "chat": {"id": 777, "type": "private"},
        "message_ids": [5, 6],
    })
    await on_deleted_business_messages(event, conn=conn)
    kinds = [r["kind"] for r in conn.execute("SELECT * FROM message_events ORDER BY id")]
    assert kinds == ["edited", "deleted", "deleted"]


def test_dispatcher_resolves_business_updates(settings):
    from aiogram import Dispatcher

    from tg_business_bridge.daemon import business_handlers, draft_handlers

    dp = Dispatcher(conn=None, settings=settings)
    dp.include_router(business_handlers.router)
    dp.include_router(draft_handlers.router)
    updates = dp.resolve_used_update_types()
    assert {"business_connection", "business_message",
            "edited_business_message", "deleted_business_messages",
            "callback_query"} <= set(updates)
