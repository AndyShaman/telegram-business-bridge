import logging
import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BRIDGE_", env_file=".env", extra="ignore")

    bot_token: str
    data_dir: Path = Path("./data")
    send_policy: Literal["approve", "auto"] = "approve"
    auto_send_chat_ids: list[int] = []
    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    deepgram_api_key: str = ""
    media_retention_days: int = 0  # 0 = хранить вечно (по умолчанию); тексты хранятся вечно всегда

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bridge.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def _secure_data_dir(data_dir: Path) -> None:
    """Каталог с личными сообщениями должен быть доступен только владельцу (0700)."""
    if data_dir.exists():
        try:
            os.chmod(data_dir, 0o700)
        except OSError as e:
            log.warning("failed to chmod data dir %s: %s", data_dir, e)
    else:
        data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)


def assert_data_dir_safe(settings: Settings) -> None:
    """БД с личными сообщениями не должна попасть в git."""
    _secure_data_dir(settings.data_dir)
    git_dir = settings.data_dir.resolve()
    for parent in [git_dir, *git_dir.parents]:
        if (parent / ".git").exists():
            gitignore = parent / ".gitignore"
            rel = str(git_dir.relative_to(parent))
            if not gitignore.exists() or rel.split("/")[0] not in gitignore.read_text():
                raise SystemExit(
                    f"ОТКАЗ ЗАПУСКА: {git_dir} лежит в git-репозитории {parent}, "
                    f"но не покрыт .gitignore. Добавь '{rel}/' в {gitignore}."
                )
            break
