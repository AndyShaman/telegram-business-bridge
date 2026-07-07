import pytest

from tg_business_bridge import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "bridge.db")
    yield c
    c.close()
