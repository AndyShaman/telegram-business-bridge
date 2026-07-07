from tg_business_bridge import db
from test_db import _msg


def _fill(conn):
    db.insert_message(conn, _msg(message_id=1, ts=100, text="обсудили договор аренды"))
    db.insert_message(conn, _msg(message_id=2, ts=200, text="пришлю счёт завтра", sender_name="Bob", sender_id=888, chat_id=888))
    db.insert_message(conn, _msg(message_id=3, ts=300, direction="out", text="ок, жду договор"))


def test_fts_search_basic(conn):
    _fill(conn)
    hits = db.search_messages(conn, "договор")
    assert {h["message_id"] for h in hits} == {1, 3}


def test_fts_search_filters(conn):
    _fill(conn)
    assert db.search_messages(conn, "договор", from_ts=250) == [
        h for h in db.search_messages(conn, "договор") if h["ts"] >= 250
    ]
    assert db.search_messages(conn, "счёт", chat_id=888)[0]["message_id"] == 2
    assert db.search_messages(conn, "счёт", chat_id=777) == []


def test_get_history_and_list_chats(conn):
    _fill(conn)
    hist = db.get_history(conn, 777)
    assert [h["message_id"] for h in hist] == [1, 3]
    hist2 = db.get_history(conn, 777, from_ts=150)
    assert [h["message_id"] for h in hist2] == [3]
    chats = db.list_chats(conn)
    assert {c["chat_id"] for c in chats} == {777, 888}
    c777 = next(c for c in chats if c["chat_id"] == 777)
    assert c777["msg_count"] == 2 and c777["last_ts"] == 300


def test_search_query_sanitized(conn):
    _fill(conn)
    assert db.search_messages(conn, "") == []
    assert db.search_messages(conn, "???") == []
    assert {h["message_id"] for h in db.search_messages(conn, "договор?")} == {1, 3}
    assert {h["message_id"] for h in db.search_messages(conn, "договор-аренды")} == {1}


def test_fts_reindexed_after_text_update(conn):
    rowid = db.insert_message(conn, _msg(message_id=9, text=None))
    assert db.search_messages(conn, "расшифровка") == []
    db.set_message_text(conn, rowid, "расшифровка голосового")
    assert [h["message_id"] for h in db.search_messages(conn, "расшифровка")] == [9]


def test_search_sender_and_limit(conn):
    _fill(conn)
    assert db.search_messages(conn, "счёт", sender="Bo")[0]["message_id"] == 2
    assert db.search_messages(conn, "счёт", sender="%") == []
    assert len(db.search_messages(conn, "договор", limit=1)) == 1
