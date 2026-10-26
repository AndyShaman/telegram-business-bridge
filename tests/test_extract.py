from aiogram.types import Message

from tg_business_bridge.extract import extract_message_row, is_bot_outgoing

BASE = {
    "message_id": 5,
    "date": 1751800000,
    "chat": {"id": 777, "type": "private", "first_name": "Alice"},
    "from": {"id": 777, "is_bot": False, "first_name": "Alice"},
    "business_connection_id": "conn1",
}


def _m(**over) -> Message:
    d = {**BASE, **over}
    return Message.model_validate(d)


def test_incoming_text():
    row = extract_message_row(_m(text="привет"), bot_id=99)
    assert row["direction"] == "in"
    assert row["media_type"] == "text"
    assert row["text"] == "привет"
    assert row["chat_id"] == 777 and row["message_id"] == 5
    assert row["connection_id"] == "conn1"
    assert row["ts"] == 1751800000
    assert '"message_id":5' in row["raw_json"].replace(" ", "")


def test_outgoing_by_owner():
    m = _m(**{"from": {"id": 42, "is_bot": False, "first_name": "Owner"}}, text="ok")
    row = extract_message_row(m, bot_id=99)
    assert row["direction"] == "out"


def test_voice_message():
    m = _m(voice={"file_id": "V1", "file_unique_id": "U1", "duration": 3, "file_size": 5000})
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "voice"
    assert row["file_id"] == "V1" and row["file_size"] == 5000
    assert row["text"] is None


def test_photo_takes_largest():
    m = _m(
        photo=[
            {"file_id": "S", "file_unique_id": "US", "width": 90, "height": 90, "file_size": 100},
            {"file_id": "L", "file_unique_id": "UL", "width": 800, "height": 800, "file_size": 9000},
        ],
        caption="смотри",
    )
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "photo" and row["file_id"] == "L"
    assert row["text"] == "смотри"


def test_away_message_marked_auto():
    m = _m(text="я не в сети", is_from_offline=True)
    assert extract_message_row(m, bot_id=99)["is_auto"] == 1


def test_bot_sent_echo_is_outgoing():
    # from.id == chat.id (looks like incoming), but sender_business_bot.id == bot_id
    # means the bot itself sent this on the owner's behalf — must be "out", not "in".
    m = _m(sender_business_bot={"id": 99, "is_bot": True, "first_name": "bridge"}, text="ok")
    row = extract_message_row(m, bot_id=99)
    assert row["direction"] == "out"


def test_is_bot_outgoing():
    m = _m(sender_business_bot={"id": 99, "is_bot": True, "first_name": "bridge"}, text="x")
    assert is_bot_outgoing(m, bot_id=99) is True
    assert is_bot_outgoing(_m(text="x"), bot_id=99) is False


def test_incoming_with_link_preview_serializes():
    # регрессия: aiogram заполняет LinkPreviewOptions сентинелами Default,
    # model_dump_json без exclude_defaults падал и входящее терялось
    m = _m(
        text="глянь https://example.com",
        link_preview_options={"url": "https://example.com"},
    )
    row = extract_message_row(m, bot_id=99)
    assert row["direction"] == "in"
    assert "example.com" in row["raw_json"]


POLL = {
    "id": "p1", "question": "Куда едем?", "total_voter_count": 0, "is_closed": False,
    "is_anonymous": True, "type": "regular", "allows_multiple_answers": False,
    "allows_revoting": True, "members_only": False,
    "options": [
        {"persistent_id": "o1", "text": "Горы", "voter_count": 0},
        {"persistent_id": "o2", "text": "Море", "voter_count": 0},
    ],
}


def test_poll_kind_and_searchable_text():
    row = extract_message_row(_m(poll=POLL), bot_id=99)
    assert row["media_type"] == "poll"
    assert row["file_id"] is None
    assert "Куда едем?" in row["text"]
    assert "Горы" in row["text"] and "Море" in row["text"]


def test_poll_found_by_fts(conn):
    from tg_business_bridge import db

    row = extract_message_row(_m(poll=POLL), bot_id=99)
    db.insert_message(conn, row)
    found = db.search_messages(conn, "Море", None, None, None, None, 10)
    assert [r["message_id"] for r in found] == [5]


def test_checklist_marks_done_tasks():
    m = _m(checklist={
        "title": "Сборы",
        "tasks": [
            {"id": 1, "text": "Паспорт", "completion_date": 1751800100},
            {"id": 2, "text": "Зарядка"},
        ],
    })
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "checklist"
    assert "Сборы" in row["text"]
    assert "[x] Паспорт" in row["text"]
    assert "[ ] Зарядка" in row["text"]


def test_paid_media_kind_keeps_caption():
    m = _m(paid_media={"star_count": 5, "paid_media": []}, caption="платный пост")
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "paid_media"
    assert row["text"] == "платный пост"


def test_contact_text_has_name_and_phone():
    m = _m(contact={"phone_number": "+375291112233", "first_name": "Иван", "last_name": "Петров"})
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "contact"
    assert "Иван" in row["text"] and "Петров" in row["text"] and "+375291112233" in row["text"]


def test_venue_text_has_title_and_address():
    m = _m(venue={
        "location": {"latitude": 53.9, "longitude": 27.5},
        "title": "Кафе Север", "address": "ул. Ленина, 1",
    })
    row = extract_message_row(m, bot_id=99)
    assert row["media_type"] == "venue"
    assert "Кафе Север" in row["text"] and "ул. Ленина, 1" in row["text"]


def test_location_kind():
    row = extract_message_row(_m(location={"latitude": 53.9, "longitude": 27.5}), bot_id=99)
    assert row["media_type"] == "location"
