"""Petite couche SQLite. Volontairement simple (peu de données, accès rares)."""

import os
import sqlite3
import datetime

# DB_PATH configurable -> important pour Railway (voir README, sinon la base
# est effacée à chaque redéploiement).
DB_PATH = os.environ.get("DB_PATH", "leaderboard.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS players(
                discord_id        INTEGER PRIMARY KEY,
                chesscom_username TEXT UNIQUE NOT NULL,
                joined_at         TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS config(
                key   TEXT PRIMARY KEY,
                value TEXT
            )"""
        )


# --- joueurs ---------------------------------------------------------------

def add_player(discord_id: int, username: str):
    """Upsert par discord_id (un membre = un seul pseudo)."""
    with _conn() as c:
        c.execute("DELETE FROM players WHERE discord_id=?", (discord_id,))
        c.execute(
            "INSERT INTO players(discord_id, chesscom_username, joined_at) VALUES (?,?,?)",
            (discord_id, username, datetime.datetime.utcnow().isoformat()),
        )


def remove_player(discord_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM players WHERE discord_id=?", (discord_id,))
        return cur.rowcount > 0


def get_by_username(username: str):
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM players WHERE chesscom_username=?", (username,)
        ).fetchone()
        return dict(r) if r else None


def list_players():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM players")]


# --- config ----------------------------------------------------------------

def set_config(key: str, value):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))


def get_config(key: str):
    with _conn() as c:
        r = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None
