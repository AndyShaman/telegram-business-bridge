import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_TIMEOUT = aiohttp.ClientTimeout(total=60)

_CONTENT_TYPES = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}


def _content_type(path: str) -> str:
    return _CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


async def transcribe_file(path: str, api_key: str) -> str | None:
    """Распознаёт речь в аудиофайле через Deepgram. Best-effort: любая ошибка -> None, без исключений."""
    try:
        audio_bytes = Path(path).read_bytes()
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                DEEPGRAM_URL,
                params={"model": "nova-3", "language": "multi", "smart_format": "true"},
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": _content_type(path),
                },
                data=audio_bytes,
            ) as resp:
                if resp.status != 200:
                    log.warning("deepgram transcription failed for %s: %s %s", path, resp.status, await resp.text())
                    return None
                data = await resp.json()
        transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        return transcript or None
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("deepgram transcription error for %s: %s", path, e)
        return None
