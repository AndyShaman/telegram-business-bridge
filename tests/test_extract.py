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
