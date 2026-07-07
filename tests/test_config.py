import os
import stat
from pathlib import Path

from tg_business_bridge.config import Settings, assert_data_dir_safe


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_SEND_POLICY", "auto")
    monkeypatch.setenv("BRIDGE_AUTO_SEND_CHAT_IDS", "[111, 222]")
    s = Settings()
    assert s.bot_token == "123:abc"
    assert s.send_policy == "auto"
    assert s.auto_send_chat_ids == [111, 222]
    assert s.db_path == tmp_path / "bridge.db"
    assert s.media_dir == tmp_path / "media"


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("BRIDGE_SEND_POLICY", raising=False)
    s = Settings()
    assert s.send_policy == "approve"
    assert s.auto_send_chat_ids == []
    assert s.mcp_transport == "stdio"
    assert s.mcp_host == "127.0.0.1"
    assert s.mcp_port == 8765
    assert s.media_retention_days == 0


def test_settings_mcp_transport_from_env(monkeypatch):
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BRIDGE_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("BRIDGE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("BRIDGE_MCP_PORT", "9000")
    s = Settings()
    assert s.mcp_transport == "streamable-http"
    assert s.mcp_host == "0.0.0.0"
    assert s.mcp_port == 9000


def test_assert_data_dir_safe_creates_dir_with_0700(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(data_dir))
    s = Settings()
    assert_data_dir_safe(s)
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_assert_data_dir_safe_tightens_loose_existing_dir(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    os.chmod(data_dir, 0o777)
    monkeypatch.setenv("BRIDGE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(data_dir))
    s = Settings()
    assert_data_dir_safe(s)
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700
