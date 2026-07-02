import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Module db branché sur une base SQLite jetable."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    return db


class FakeClient:
    """Client chess.com factice : réponses pré-câblées, appels enregistrés."""

    def __init__(self, stats=None, archives=None, fail=None):
        self.stats = stats or {}
        self.archives = archives or {}  # {(username, year, month): archive}
        self.fail = fail or set()       # usernames dont get_stats explose
        self.calls = []

    async def get_stats(self, username):
        self.calls.append(("stats", username))
        if username in self.fail:
            raise RuntimeError("boom")
        return self.stats.get(username)

    async def get_archive(self, username, year, month):
        self.calls.append(("archive", username, year, month))
        return self.archives.get((username, year, month))
