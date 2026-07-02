"""Petite couche SQLite. Volontairement simple (peu de données, accès rares).

Timestamps stockés en UTC ISO 8601 : les comparaisons SQLite sont des
comparaisons de chaînes, mélanger les fuseaux casserait l'ordre.
"""

import os
import gzip
import json
import sqlite3
import datetime

# DB_PATH configurable -> important pour Railway (voir README, sinon la base
# est effacée à chaque redéploiement).
DB_PATH = os.environ.get("DB_PATH", "leaderboard.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
        c.execute(
            """CREATE TABLE IF NOT EXISTS rating_history(
                username TEXT NOT NULL,
                taken_at TEXT NOT NULL,
                rapid    INTEGER,
                blitz    INTEGER,
                bullet   INTEGER,
                PRIMARY KEY (username, taken_at)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS announced_games(
                game_id      TEXT PRIMARY KEY,
                announced_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS http_cache(
                url           TEXT PRIMARY KEY,
                etag          TEXT,
                last_modified TEXT,
                body          BLOB NOT NULL,
                fetched_at    TEXT NOT NULL,
                last_used_at  TEXT NOT NULL
            )"""
        )


# --- joueurs ---------------------------------------------------------------

def add_player(discord_id: int, username: str):
    """Upsert par discord_id (un membre = un seul pseudo)."""
    with _conn() as c:
        c.execute("DELETE FROM players WHERE discord_id=?", (discord_id,))
        c.execute(
            "INSERT INTO players(discord_id, chesscom_username, joined_at) VALUES (?,?,?)",
            (discord_id, username, _utcnow_iso()),
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


def get_by_discord_id(discord_id: int):
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM players WHERE discord_id=?", (discord_id,)
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


# --- historique de ratings (deltas Elo, board progression) ------------------

def save_rating_snapshot(username: str, rapid, blitz, bullet, taken_at: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO rating_history VALUES (?,?,?,?,?)",
            (username, taken_at, rapid, blitz, bullet),
        )


def get_baseline_ratings(username: str, week_start_utc_iso: str):
    """Ratings de référence pour les deltas de la semaine : dernier snapshot
    AVANT le début de semaine, à défaut le premier APRÈS (membre inscrit en
    cours de semaine -> delta partiel mais correct)."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM rating_history WHERE username=? AND taken_at<? "
            "ORDER BY taken_at DESC LIMIT 1",
            (username, week_start_utc_iso),
        ).fetchone()
        if not r:
            r = c.execute(
                "SELECT * FROM rating_history WHERE username=? AND taken_at>=? "
                "ORDER BY taken_at ASC LIMIT 1",
                (username, week_start_utc_iso),
            ).fetchone()
        return dict(r) if r else None


def purge_rating_history(keep_days: int = 90):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=keep_days)
    ).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM rating_history WHERE taken_at<?", (cutoff,))


# --- dédup des annonces de parties ------------------------------------------

def is_game_announced(game_id: str) -> bool:
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM announced_games WHERE game_id=?", (game_id,)
        ).fetchone()
        return r is not None


def mark_games_announced(game_ids):
    now = _utcnow_iso()
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO announced_games VALUES (?,?)",
            [(gid, now) for gid in game_ids],
        )


# --- cache HTTP conditionnel (archives chess.com) ----------------------------

def cache_get(url: str):
    with _conn() as c:
        r = c.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        if not r:
            return None
        c.execute(
            "UPDATE http_cache SET last_used_at=? WHERE url=?", (_utcnow_iso(), url)
        )
        return {
            "etag": r["etag"],
            "last_modified": r["last_modified"],
            "body": json.loads(gzip.decompress(r["body"])),
        }


def cache_set(url: str, etag, last_modified, body):
    now = _utcnow_iso()
    blob = gzip.compress(json.dumps(body).encode())
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?)",
            (url, etag, last_modified, blob, now, now),
        )


def cache_purge(days: int = 60):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    ).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM http_cache WHERE last_used_at<?", (cutoff,))


class SqliteHttpCache:
    """Adaptateur pour ChessComClient (qui ne doit pas importer ce module)."""

    def get(self, url):
        return cache_get(url)

    def set(self, url, etag, last_modified, body):
        cache_set(url, etag, last_modified, body)
