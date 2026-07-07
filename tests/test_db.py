import os
import stat

from tg_business_bridge import db


def _msg(**over):
    row = dict(
        connection_id="conn1", chat_id=777, message_id=1, sender_id=777,
        sender_name="Alice", ts=1000, direction="in", is_auto=0,
        media_type="text", text="привет", raw_json="{}",
    )
    row.update(over)
    return row


def test_insert_and_read_message(conn):
    rowid = db.insert_message(conn, _msg())
    got = conn.execute("SELECT * FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["chat_id"] == 777
    assert got["text"] == "привет"
    assert got["raw_json"] == "{}"


def test_set_media_path(conn):
    rowid = db.insert_message(conn, _msg(media_type="photo", file_id="F1"))
    db.set_media_path(conn, rowid, "media/2026/x.jpg")
    got = conn.execute("SELECT media_path FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["media_path"] == "media/2026/x.jpg"


def test_set_message_text(conn):
    rowid = db.insert_message(conn, _msg(message_id=1, text=None))
    # вторая строка с тем же chat_id+message_id — пара не уникальна, адресация по rowid
    other = db.insert_message(conn, _msg(message_id=1, text="другое"))
    db.set_message_text(conn, rowid, "расшифровка голосового")
    got = conn.execute("SELECT text FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["text"] == "расшифровка голосового"
    untouched = conn.execute("SELECT text FROM messages WHERE id=?", (other,)).fetchone()
    assert untouched["text"] == "другое"


def test_connection_upsert_and_get(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": true}', True)
    db.upsert_connection(conn, "c1", 42, '{"can_reply": false}', False)
    assert db.get_enabled_connection(conn) is None
    db.upsert_connection(conn, "c2", 42, '{"can_reply": true}', True)
    assert db.get_enabled_connection(conn)["connection_id"] == "c2"


def test_get_enabled_connection_prefers_newer_updated_ts(conn):
    db.upsert_connection(conn, "c1", 42, '{"can_reply": true}', True)
    db.upsert_connection(conn, "c2", 42, '{"can_reply": true}', True)
    conn.execute("UPDATE connections SET updated_ts=100 WHERE connection_id='c1'")
    conn.execute("UPDATE connections SET updated_ts=200 WHERE connection_id='c2'")
    conn.commit()
    assert db.get_enabled_connection(conn)["connection_id"] == "c2"


def test_message_events(conn):
    db.add_message_event(conn, 777, 5, "deleted", 2000)
    ev = conn.execute("SELECT * FROM message_events").fetchone()
    assert ev["kind"] == "deleted" and ev["new_text"] is None


def test_draft_lifecycle(conn):
    did = db.create_draft(conn, 777, "ответ", "pending")
    assert db.get_draft(conn, did)["status"] == "pending"
    assert len(db.get_drafts_by_status(conn, "pending")) == 1
    db.set_draft_status(conn, did, "failed", error="boom")
    row = db.get_draft(conn, did)
    assert row["status"] == "failed" and row["error"] == "boom"


def test_last_incoming_message_id(conn):
    db.insert_message(conn, _msg(message_id=10, direction="in", ts=1))
    db.insert_message(conn, _msg(message_id=11, direction="out", ts=2))
    assert db.last_incoming_message_id(conn, 777) == 10
    assert db.last_incoming_message_id(conn, 999) is None


def test_connect_secures_db_file(tmp_path):
    db_path = tmp_path / "bridge.db"
    c = db.connect(db_path)
    try:
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
        wal_path = db_path.with_name(db_path.name + "-wal")
        if wal_path.exists():
            assert stat.S_IMODE(os.stat(wal_path).st_mode) == 0o600
    finally:
        c.close()


def test_get_context_returns_anchor_and_radius(conn):
    for i in range(1, 12):
        db.insert_message(conn, _msg(message_id=i, ts=1000 + i))
    rows = db.get_context(conn, 777, 6, radius=2)
    assert [r["message_id"] for r in rows] == [4, 5, 6, 7, 8]


def test_get_context_respects_chat_boundary(conn):
    db.insert_message(conn, _msg(chat_id=1, message_id=1, ts=1000))
    db.insert_message(conn, _msg(chat_id=2, message_id=1, ts=1001))
    db.insert_message(conn, _msg(chat_id=1, message_id=2, ts=1002))
    rows = db.get_context(conn, 1, 2, radius=5)
    assert [(r["chat_id"], r["message_id"]) for r in rows] == [(1, 1), (1, 2)]


def test_get_context_edge_of_history(conn):
    for i in range(1, 4):
        db.insert_message(conn, _msg(message_id=i, ts=1000 + i))
    rows = db.get_context(conn, 777, 1, radius=5)
    assert [r["message_id"] for r in rows] == [1, 2, 3]


def test_get_context_missing_anchor(conn):
    assert db.get_context(conn, 777, 999, radius=5) == []


def test_get_context_includes_null_message_id_at_same_ts(conn):
    db.insert_message(conn, _msg(message_id=None, ts=1000))
    db.insert_message(conn, _msg(message_id=10, ts=1000))
    rows = db.get_context(conn, 777, 10, radius=5)
    assert [r["message_id"] for r in rows] == [None, 10]


def test_list_expired_media(conn):
    old_id = db.insert_message(conn, _msg(message_id=1, ts=1000, media_type="photo", file_id="F1"))
    db.set_media_path(conn, old_id, "media/2020-01/x.jpg")
    fresh_id = db.insert_message(conn, _msg(message_id=2, ts=5000, media_type="photo", file_id="F2"))
    db.set_media_path(conn, fresh_id, "media/2020-01/y.jpg")
    db.insert_message(conn, _msg(message_id=3, ts=1000))  # без медиа — не должен попасть

    rows = db.list_expired_media(conn, cutoff_ts=2000)
    assert [r["id"] for r in rows] == [old_id]


def test_clear_media_path(conn):
    rowid = db.insert_message(conn, _msg(media_type="photo", file_id="F1", raw_json='{"a":1}'))
    db.set_media_path(conn, rowid, "media/2020-01/x.jpg")
    db.clear_media_path(conn, rowid)
    got = conn.execute("SELECT media_path, file_id, raw_json FROM messages WHERE id=?", (rowid,)).fetchone()
    assert got["media_path"] is None
    assert got["file_id"] == "F1"
    assert got["raw_json"] == '{"a":1}'


def test_claim_draft(conn):
    did = db.create_draft(conn, 777, "ответ", "approved")
    assert db.claim_draft(conn, did) is True
    assert db.get_draft(conn, did)["status"] == "sending"
    # уже забрано — повторный claim не проходит
    assert db.claim_draft(conn, did) is False
