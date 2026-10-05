import os
import sys
import tempfile

import pytest

# Make the project root importable and give every test session its own DB.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="dosimetry-test-"), "test.db")
os.environ["DOSIMETRY_DB"] = _TMP_DB


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_db():
    """Wipe all rows before each test for isolation."""
    from app import db

    db.init_db()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM deliveries")
        conn.execute("DELETE FROM channel_state")
        conn.execute("DELETE FROM courses")
        conn.commit()
    finally:
        conn.close()
    yield


def make_course(client, course_id="c1", channels=None):
    channels = channels or [
        {"name": "A", "prescribed": "10"},
        {"name": "B", "prescribed": "5.5"},
    ]
    r = client.put(f"/api/courses/{course_id}", json={"channels": channels})
    assert r.status_code == 201, r.text
    return r
