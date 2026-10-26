import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


class ChatPaused(Exception):
    """Чат на паузе: черновик не создан."""

_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def _fts_query(query: str) -> str | None:
    """Пользовательский запрос → безопасное FTS5-выражение: слова как литералы (все должны встретиться)."""
    tokens = _FTS_TOKEN.findall(query)
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    connection_id TEXT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    sender_id INTEGER,
    sender_name TEXT,
    ts INTEGER NOT NULL,
    direction TEXT NOT NULL DEFAULT 'in',
    is_auto INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    text TEXT,
    media_path TEXT,
    file_id TEXT,
    file_unique_id TEXT,
    file_size INTEGER,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    rights_json TEXT NOT NULL,
    is_enabled INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS message_events (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts INTEGER NOT NULL,
    new_text TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_ts INTEGER NOT NULL,
    card_message_id INTEGER,
    edit_prompt_message_id INTEGER,
    parse_mode TEXT,
    original_text TEXT,
    original_parse_mode TEXT
);
CREATE TABLE IF NOT EXISTS draft_attachments (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    file_id TEXT,
    path TEXT,
    filename TEXT,
    file_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_draft_attachments_draft ON draft_attachments(draft_id, position);
CREATE TABLE IF NOT EXISTS paused_chats (
    chat_id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, coalesce(new.text, ''));
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, coalesce(old.text, ''));
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, coalesce(new.text, ''));
END;
"""

MESSAGE_COLS = [
    "connection_id", "chat_id", "message_id", "sender_id", "sender_name",
    "ts", "direction", "is_auto", "media_type", "text", "media_path",
    "file_id", "file_unique_id", "file_size", "raw_json",
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    init_schema(conn)
    _secure_db_files(db_path)
    return conn


def _secure_db_files(db_path: Path) -> None:
    """Файл БД и его -wal/-shm не должны быть читаемы другими пользователями (0600)."""
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError as e:
                log.warning("failed to chmod db file %s: %s", path, e)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # БД, созданные до появления редактора черновиков, колонки не имеют — CREATE TABLE
    # IF NOT EXISTS их не обновит (r[1] — имя колонки в PRAGMA table_info)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    for name, decl in (
        ("edit_prompt_message_id", "INTEGER"), ("parse_mode", "TEXT"),
        ("original_text", "TEXT"), ("original_parse_mode", "TEXT"),
    ):
        if name in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE drafts ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as e:
            # демон и MCP-сервер стартуют независимо: колонку мог добавить сосед
            if "duplicate column name" not in str(e):
                raise
    conn.commit()


def insert_message(conn: sqlite3.Connection, row: dict) -> int:
    values = {c: row.get(c) for c in MESSAGE_COLS}
    if values["is_auto"] is None:
        values["is_auto"] = 0
    placeholders = ", ".join(f":{c}" for c in MESSAGE_COLS)
    cur = conn.execute(
        f"INSERT INTO messages ({', '.join(MESSAGE_COLS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cur.lastrowid


def set_media_path(conn: sqlite3.Connection, rowid: int, media_path: str) -> None:
    conn.execute("UPDATE messages SET media_path=? WHERE id=?", (media_path, rowid))
    conn.commit()


def list_expired_media(conn: sqlite3.Connection, cutoff_ts: int) -> list[sqlite3.Row]:
    """Строки со скачанным медиа старше cutoff_ts (для очистки хранилища).

    file_unique_id стабилен у Telegram, поэтому два сообщения могут ссылаться на один
    и тот же файл на диске. Строки, чей media_path разделён с более свежим сообщением,
    не возвращаются: удалять такой файл нельзя, а media_path устаревшей строки остаётся
    валидным, пока файл жив."""
    return conn.execute(
        """SELECT * FROM messages m
           WHERE m.media_path IS NOT NULL AND m.ts < :cutoff
             AND NOT EXISTS (
               SELECT 1 FROM messages f
               WHERE f.media_path = m.media_path AND f.ts >= :cutoff
             )""",
        {"cutoff": cutoff_ts},
    ).fetchall()


def clear_media_path(conn: sqlite3.Connection, rowid: int) -> None:
    """Обнуляет media_path после удаления файла с диска. file_id/file_unique_id/raw_json
    и текст сообщения не трогаются — тексты хранятся вечно, а файл можно перекачать
    через Telegram, пока он жив на их серверах."""
    conn.execute("UPDATE messages SET media_path=NULL WHERE id=?", (rowid,))
    conn.commit()


def set_message_text(conn: sqlite3.Connection, rowid: int, text: str) -> None:
    """Проставляет текст сообщения постфактум (например, после транскрибации голосового).
    Адресация по rowid: пара chat_id+message_id схемой не уникальна."""
    conn.execute("UPDATE messages SET text=? WHERE id=?", (text, rowid))
    conn.commit()


def upsert_connection(
    conn: sqlite3.Connection, connection_id: str, owner_id: int,
    rights_json: str, is_enabled: bool,
) -> None:
    conn.execute(
        """INSERT INTO connections (connection_id, owner_id, rights_json, is_enabled, updated_ts)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(connection_id) DO UPDATE SET
             owner_id=excluded.owner_id, rights_json=excluded.rights_json,
             is_enabled=excluded.is_enabled, updated_ts=excluded.updated_ts""",
        (connection_id, owner_id, rights_json, int(is_enabled), int(time.time())),
    )
    conn.commit()


def get_connection(conn: sqlite3.Connection, connection_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM connections WHERE connection_id=?", (connection_id,)
    ).fetchone()


def get_enabled_connection(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM connections WHERE is_enabled=1 "
        "ORDER BY updated_ts DESC, connection_id DESC LIMIT 1"
    ).fetchone()


def add_message_event(
    conn: sqlite3.Connection, chat_id: int, message_id: int, kind: str,
    ts: int, new_text: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO message_events (chat_id, message_id, kind, ts, new_text) VALUES (?, ?, ?, ?, ?)",
        (chat_id, message_id, kind, ts, new_text),
    )
    conn.commit()


_ATTACHMENT_COLS = ["kind", "file_id", "path", "filename", "file_size"]


def create_draft(
    conn: sqlite3.Connection, chat_id: int, text: str, status: str,
    parse_mode: str | None = None,
    attachments: Callable[[int], list[dict]] | None = None,
) -> int:
    """parse_mode: None — обычный текст, 'HTML' — разметка Telegram HTML.

    attachments(draft_id) -> записи вложений (kind, file_id | path, filename, file_size):
    вызывается внутри той же транзакции, до commit — демон не увидит черновик, пока
    файлы вложений не записаны. Исключение из него откатывает и сам черновик."""
    try:
        # проверка паузы и вставка — одна запись: пауза, поставленная после проверки
        # в MCP, но до вставки, черновик всё равно не пропустит
        cur = conn.execute(
            """INSERT INTO drafts (chat_id, text, status, created_ts, parse_mode)
               SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM paused_chats WHERE chat_id=?)""",
            (chat_id, text, status, int(time.time()), parse_mode, chat_id),
        )
        if cur.rowcount != 1:
            raise ChatPaused(chat_id)
        did = cur.lastrowid
        if attachments is not None:
            for pos, item in enumerate(attachments(did)):
                conn.execute(
                    f"INSERT INTO draft_attachments (draft_id, position, {', '.join(_ATTACHMENT_COLS)}) "
                    f"VALUES (?, ?, {', '.join('?' for _ in _ATTACHMENT_COLS)})",
                    (did, pos, *(item.get(c) for c in _ATTACHMENT_COLS)),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return did


def get_draft_attachments(conn: sqlite3.Connection, draft_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM draft_attachments WHERE draft_id=? ORDER BY position", (draft_id,)
    ).fetchall()]


def get_message(conn: sqlite3.Connection, chat_id: int, message_id: int) -> sqlite3.Row | None:
    """Последняя (по id) строка с этой парой chat_id+message_id — пара схемой не уникальна."""
    return conn.execute(
        "SELECT * FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
        (chat_id, message_id),
    ).fetchone()


def get_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def get_drafts_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drafts WHERE status=? ORDER BY id", (status,)
    ).fetchall()


def set_draft_status(
    conn: sqlite3.Connection, draft_id: int, status: str, error: str | None = None,
) -> None:
    conn.execute("UPDATE drafts SET status=?, error=? WHERE id=?", (status, error, draft_id))
    conn.commit()


def set_draft_status_if(
    conn: sqlite3.Connection, draft_id: int, from_status: str, to_status: str,
    error: str | None = None,
) -> bool:
    """Атомарно переводит черновик from_status -> to_status. False, если статус уже
    конкурентно изменился (TOCTOU-защита между чтением черновика и записью нового статуса)."""
    cur = conn.execute(
        "UPDATE drafts SET status=?, error=? WHERE id=? AND status=?",
        (to_status, error, draft_id, from_status),
    )
    conn.commit()
    return cur.rowcount == 1


def update_draft_text(
    conn: sqlite3.Connection, draft_id: int, text: str, parse_mode: str | None = None,
) -> bool:
    """Заменяет текст и разметку черновика, пока тот ждёт решения владельца. False, если
    статус уже не 'awaiting': правка не должна догонять отправленный или заменённый черновик.
    Первая версия агента сохраняется в original_text/original_parse_mode при первой правке
    (в SET SQLite видит старые значения строки, так что повторная правка её не перетрёт)."""
    cur = conn.execute(
        """UPDATE drafts SET
             original_parse_mode = CASE WHEN original_text IS NULL THEN parse_mode
                                        ELSE original_parse_mode END,
             original_text = COALESCE(original_text, text),
             text=?, parse_mode=?
           WHERE id=? AND status='awaiting'""",
        (text, parse_mode, draft_id),
    )
    conn.commit()
    return cur.rowcount == 1


def set_draft_card(conn: sqlite3.Connection, draft_id: int, message_id: int) -> None:
    conn.execute("UPDATE drafts SET card_message_id=? WHERE id=?", (message_id, draft_id))
    conn.commit()


def set_edit_prompt(conn: sqlite3.Connection, draft_id: int, message_id: int | None) -> None:
    """Запоминает (или забывает, при None) сообщение-приглашение открыть редактор —
    чтобы удалить его, когда оно перестанет быть нужным."""
    conn.execute("UPDATE drafts SET edit_prompt_message_id=? WHERE id=?", (message_id, draft_id))
    conn.commit()


def claim_draft(conn: sqlite3.Connection, draft_id: int) -> bool:
    """Атомарно переводит approved-черновик в sending. False, если уже забрал другой процесс."""
    cur = conn.execute(
        "UPDATE drafts SET status = 'sending' WHERE id = ? AND status = 'approved'", (draft_id,)
    )
    conn.commit()
    return cur.rowcount == 1


def recover_stale_sending(conn: sqlite3.Connection) -> int:
    """При старте демона возвращает 'sending' в 'approved': эти черновики застряли из-за
    падения демона между claim_draft и фактической отправкой (в момент старта отправка
    в процессе идти не может).

    Осознанный компромисс: если демон упал в узкое окно между принятием сообщения
    Telegram'ом и записью статуса 'sent', после рестарта черновик уйдёт повторно
    (дубль у собеседника). Принято как меньшее зло: альтернатива — помечать failed
    и требовать ручного переодобрения после каждого падения во время отправки."""
    cur = conn.execute("UPDATE drafts SET status='approved' WHERE status='sending'")
    conn.commit()
    return cur.rowcount


def list_drafts(
    conn: sqlite3.Connection, chat_id: int | None = None, limit: int = 20,
) -> list[dict]:
    sql = ("SELECT id, chat_id, text, status, error, created_ts, card_message_id, parse_mode "
           "FROM drafts")
    params: dict = {"limit": limit}
    if chat_id is not None:
        sql += " WHERE chat_id = :chat_id"
        params["chat_id"] = chat_id
    sql += " ORDER BY id DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def supersede_awaiting(
    conn: sqlite3.Connection, chat_id: int, except_draft_id: int,
) -> list[sqlite3.Row]:
    """Помечает более старые 'awaiting'-черновики этого чата как 'superseded' и возвращает
    только те, что реально обновились. Гвардированный UPDATE построчно (WHERE id=? AND
    status='awaiting') — черновик, чей статус конкурентно уже сменился между SELECT и
    UPDATE, не трогается и не попадает в результат."""
    candidates = conn.execute(
        "SELECT * FROM drafts WHERE chat_id=? AND status='awaiting' AND id < ?",
        (chat_id, except_draft_id),
    ).fetchall()
    updated = []
    for row in candidates:
        cur = conn.execute(
            "UPDATE drafts SET status='superseded' WHERE id=? AND status='awaiting'",
            (row["id"],),
        )
        if cur.rowcount == 1:
            updated.append(row)
    # commit безусловный: даже при пустом updated SELECT открыл транзакцию
    conn.commit()
    return updated


def list_edited_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Отправленные черновики, которые владелец правил перед отправкой, новые первыми."""
    return conn.execute(
        "SELECT * FROM drafts WHERE status='sent' AND original_text IS NOT NULL ORDER BY id DESC"
    ).fetchall()


def pause_chat(conn: sqlite3.Connection, chat_id: int) -> list[int]:
    """Ставит чат на паузу и отклоняет его ещё не отправленные черновики (созданные до
    паузы). Возвращает их id — вызывающий убирает их вложения."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO paused_chats (chat_id, ts) VALUES (?, ?)", (chat_id, int(time.time()))
        )
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM drafts WHERE chat_id=? AND status IN ('pending', 'awaiting', 'approved')",
            (chat_id,),
        ).fetchall()]
        conn.executemany(
            "UPDATE drafts SET status='rejected' WHERE id=? AND status IN ('pending', 'awaiting', 'approved')",
            [(i,) for i in ids],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ids


def unpause_chat(conn: sqlite3.Connection, chat_id: int) -> bool:
    cur = conn.execute("DELETE FROM paused_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    return cur.rowcount == 1


def is_chat_paused(conn: sqlite3.Connection, chat_id: int) -> bool:
    return conn.execute("SELECT 1 FROM paused_chats WHERE chat_id=?", (chat_id,)).fetchone() is not None


def list_paused_chats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT p.chat_id, p.ts,
                  (SELECT sender_name FROM messages m WHERE m.chat_id = p.chat_id
                   AND m.direction='in' ORDER BY ts DESC, id DESC LIMIT 1) AS sender_name,
                  (SELECT json_extract(raw_json, '$.from_user.username') FROM messages m
                   WHERE m.chat_id = p.chat_id AND m.direction='in'
                   ORDER BY ts DESC, id DESC LIMIT 1) AS username
           FROM paused_chats p ORDER BY p.ts, p.chat_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def last_incoming_message_id(conn: sqlite3.Connection, chat_id: int) -> int | None:
    row = conn.execute(
        "SELECT message_id FROM messages WHERE chat_id=? AND direction='in' ORDER BY ts DESC, id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return row["message_id"] if row else None


def last_incoming_sender_name(conn: sqlite3.Connection, chat_id: int) -> str | None:
    row = conn.execute(
        "SELECT sender_name FROM messages WHERE chat_id=? AND direction='in' "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return row["sender_name"] if row else None


def find_message_rowid(conn: sqlite3.Connection, chat_id: int, message_id: int) -> int | None:
    """Последняя (по id) строка с этой парой chat_id+message_id — пара схемой не уникальна."""
    row = conn.execute(
        "SELECT id FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
        (chat_id, message_id),
    ).fetchone()
    return row["id"] if row else None


def list_chats(conn: sqlite3.Connection, active_since_ts: int | None = None) -> list[dict]:
    sql = """SELECT chat_id,
                    max(ts) AS last_ts,
                    count(*) AS msg_count,
                    (SELECT sender_name FROM messages m2
                     WHERE m2.chat_id = m.chat_id AND m2.direction='in'
                     ORDER BY ts DESC LIMIT 1) AS last_sender_name,
                    (SELECT json_extract(raw_json, '$.from_user.username') FROM messages m3
                     WHERE m3.chat_id = m.chat_id AND m3.direction='in'
                     ORDER BY ts DESC LIMIT 1) AS username,
                    EXISTS (SELECT 1 FROM paused_chats p WHERE p.chat_id = m.chat_id) AS paused
             FROM messages m GROUP BY chat_id"""
    if active_since_ts is not None:
        sql += " HAVING max(ts) >= :since"
    sql += " ORDER BY last_ts DESC"
    rows = conn.execute(sql, {"since": active_since_ts}).fetchall()
    return [dict(r) for r in rows]


def get_history(
    conn: sqlite3.Connection, chat_id: int, from_ts: int | None = None,
    to_ts: int | None = None, limit: int = 100,
) -> list[dict]:
    sql = """SELECT message_id, ts, direction, sender_name, media_type, text, media_path
             FROM messages WHERE chat_id = :chat_id"""
    params: dict = {"chat_id": chat_id, "limit": limit}
    if from_ts is not None:
        sql += " AND ts >= :from_ts"
        params["from_ts"] = from_ts
    if to_ts is not None:
        sql += " AND ts <= :to_ts"
        params["to_ts"] = to_ts
    # Хвост ленты, как в мессенджере: LIMIT отбирает самые свежие сообщения диапазона,
    # наружу результат отдаётся по возрастанию времени
    sql += " ORDER BY ts DESC, id DESC LIMIT :limit"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_context(
    conn: sqlite3.Connection, chat_id: int, message_id: int, radius: int = 5,
) -> list[dict]:
    """Анкорное сообщение плюс до radius сообщений до и после него в том же чате,
    по возрастанию (ts, message_id). Пустой список, если анкор не найден."""
    anchor = conn.execute(
        "SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id)
    ).fetchone()
    if anchor is None:
        return []
    params = {"chat_id": chat_id, "ts": anchor["ts"], "mid": anchor["message_id"]}
    # message_id может быть NULL по схеме; NULL в сравнении выбросил бы строку
    # из обеих половин окна — приводим к -1 (раньше любых реальных id)
    before = conn.execute(
        """SELECT * FROM messages WHERE chat_id=:chat_id
           AND (ts < :ts OR (ts = :ts AND COALESCE(message_id, -1) <= :mid))
           ORDER BY ts DESC, COALESCE(message_id, -1) DESC LIMIT :limit""",
        {**params, "limit": radius + 1},
    ).fetchall()
    after = conn.execute(
        """SELECT * FROM messages WHERE chat_id=:chat_id
           AND (ts > :ts OR (ts = :ts AND COALESCE(message_id, -1) > :mid))
           ORDER BY ts ASC, COALESCE(message_id, -1) ASC LIMIT :limit""",
        {**params, "limit": radius},
    ).fetchall()
    rows = list(reversed(before)) + list(after)
    return [dict(r) for r in rows]


def search_messages(
    conn: sqlite3.Connection, query: str, chat_id: int | None = None,
    sender: str | None = None, from_ts: int | None = None,
    to_ts: int | None = None, limit: int = 20,
) -> list[dict]:
    fts_q = _fts_query(query)
    if fts_q is None:
        return []
    sql = """SELECT m.chat_id, m.message_id, m.ts, m.direction, m.sender_name,
                    m.media_type, m.text, m.media_path
             FROM messages_fts f JOIN messages m ON m.id = f.rowid
             WHERE messages_fts MATCH :q"""
    params: dict = {"q": fts_q, "limit": limit}
    if chat_id is not None:
        sql += " AND m.chat_id = :chat_id"
        params["chat_id"] = chat_id
    if sender is not None:
        esc = sender.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        sql += r" AND m.sender_name LIKE :sender ESCAPE '\'"
        params["sender"] = f"%{esc}%"
    if from_ts is not None:
        sql += " AND m.ts >= :from_ts"
        params["from_ts"] = from_ts
    if to_ts is not None:
        sql += " AND m.ts <= :to_ts"
        params["to_ts"] = to_ts
    sql += " ORDER BY m.ts DESC, m.id DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
