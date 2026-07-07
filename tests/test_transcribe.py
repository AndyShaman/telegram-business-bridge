from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tg_business_bridge.transcribe import _content_type, transcribe_file


@pytest.mark.parametrize("path,expected", [
    ("f.oga", "audio/ogg"),
    ("f.ogg", "audio/ogg"),
    ("f.mp3", "audio/mpeg"),
    ("f.m4a", "audio/mp4"),
    ("f.mp4", "audio/mp4"),
    ("f.bin", "application/octet-stream"),
])
def test_content_type_mapping(path, expected):
    assert _content_type(path) == expected


def _mock_session(status=200, json_data=None, text_data=""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)

    post_ctx = MagicMock()
    post_ctx.__aenter__ = AsyncMock(return_value=resp)
    post_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=post_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


@pytest.mark.asyncio
async def test_transcribe_happy_path(tmp_path):
    audio = tmp_path / "f.oga"
    audio.write_bytes(b"audio-bytes")
    payload = {"results": {"channels": [{"alternatives": [{"transcript": "привет мир"}]}]}}
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession", return_value=_mock_session(200, payload)):
        result = await transcribe_file(str(audio), "key")
    assert result == "привет мир"


@pytest.mark.asyncio
async def test_transcribe_error_status_returns_none(tmp_path):
    audio = tmp_path / "f.oga"
    audio.write_bytes(b"audio-bytes")
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession", return_value=_mock_session(500, text_data="boom")):
        result = await transcribe_file(str(audio), "key")
    assert result is None


@pytest.mark.asyncio
async def test_transcribe_empty_transcript_returns_none(tmp_path):
    audio = tmp_path / "f.oga"
    audio.write_bytes(b"audio-bytes")
    payload = {"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession", return_value=_mock_session(200, payload)):
        result = await transcribe_file(str(audio), "key")
    assert result is None


@pytest.mark.asyncio
async def test_transcribe_unexpected_payload_returns_none(tmp_path):
    audio = tmp_path / "f.oga"
    audio.write_bytes(b"audio-bytes")
    with patch("tg_business_bridge.transcribe.aiohttp.ClientSession", return_value=_mock_session(200, {"unexpected": True})):
        result = await transcribe_file(str(audio), "key")
    assert result is None


@pytest.mark.asyncio
async def test_transcribe_missing_file_returns_none():
    result = await transcribe_file("/nonexistent/path.oga", "key")
    assert result is None
