def test_players_roundtrip(tmp_db):
    tmp_db.add_player(1, "alice")
    assert tmp_db.get_by_discord_id(1)["chesscom_username"] == "alice"
    assert tmp_db.get_by_username("alice")["discord_id"] == 1
    # upsert : un membre = un seul pseudo
    tmp_db.add_player(1, "alice2")
    assert tmp_db.get_by_username("alice") is None
    assert tmp_db.remove_player(1) is True
    assert tmp_db.remove_player(1) is False


def test_config_roundtrip(tmp_db):
    assert tmp_db.get_config("k") is None
    tmp_db.set_config("k", 42)
    assert tmp_db.get_config("k") == "42"


def test_baseline_prefers_snapshot_before_week_start(tmp_db):
    tmp_db.save_rating_snapshot("alice", 1500, None, None, "2026-06-28T12:00:00+00:00")
    tmp_db.save_rating_snapshot("alice", 1520, None, None, "2026-06-30T12:00:00+00:00")
    base = tmp_db.get_baseline_ratings("alice", "2026-06-28T22:00:00+00:00")
    assert base["rapid"] == 1500


def test_baseline_falls_back_to_first_after(tmp_db):
    # membre inscrit en cours de semaine : premier snapshot après le début
    tmp_db.save_rating_snapshot("alice", 1520, None, None, "2026-06-30T12:00:00+00:00")
    tmp_db.save_rating_snapshot("alice", 1540, None, None, "2026-07-01T12:00:00+00:00")
    base = tmp_db.get_baseline_ratings("alice", "2026-06-28T22:00:00+00:00")
    assert base["rapid"] == 1520


def test_baseline_missing(tmp_db):
    assert tmp_db.get_baseline_ratings("alice", "2026-06-28T22:00:00+00:00") is None


def test_purge_rating_history(tmp_db):
    tmp_db.save_rating_snapshot("alice", 1500, None, None, "2020-01-01T00:00:00+00:00")
    tmp_db.purge_rating_history(keep_days=0)
    assert tmp_db.get_baseline_ratings("alice", "2030-01-01T00:00:00+00:00") is None


def test_announced_games_dedup(tmp_db):
    assert tmp_db.is_game_announced("g1") is False
    tmp_db.mark_games_announced(["g1", "g2"])
    tmp_db.mark_games_announced(["g1"])  # idempotent
    assert tmp_db.is_game_announced("g1") is True
    assert tmp_db.is_game_announced("g2") is True
    assert tmp_db.is_game_announced("g3") is False


def test_cache_roundtrip(tmp_db):
    assert tmp_db.cache_get("http://x") is None
    body = {"games": [{"a": 1, "pgn": "1. e4 e5"}]}
    tmp_db.cache_set("http://x", '"etag"', "lundi", body)
    got = tmp_db.cache_get("http://x")
    assert got["body"] == body
    assert got["etag"] == '"etag"'
    assert got["last_modified"] == "lundi"


def test_cache_purge(tmp_db):
    tmp_db.cache_set("http://x", None, None, {})
    tmp_db.cache_purge(days=0)
    assert tmp_db.cache_get("http://x") is None


def test_sqlite_http_cache_adapter(tmp_db):
    cache = tmp_db.SqliteHttpCache()
    cache.set("http://y", "e", None, {"n": 1})
    assert cache.get("http://y")["body"] == {"n": 1}
