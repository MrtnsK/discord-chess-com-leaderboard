import datetime

import pytest

import leaderboards as lb
from conftest import FakeClient


def dt(y, mo, d, h=12, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=lb.TZ)


def ts(y, mo, d, h=12, mi=0):
    return int(dt(y, mo, d, h, mi).timestamp())


# Mercredi 1er juillet 2026 : la semaine (lundi 29 juin) est à cheval sur 2 mois.
NOW = dt(2026, 7, 1)


def game(white, black, white_result, black_result, end, time_class="rapid", uuid=None):
    uuid = uuid or f"{white}-{black}-{end}"
    return {
        "uuid": uuid,
        "url": f"https://www.chess.com/game/live/{uuid}",
        "end_time": end,
        "time_class": time_class,
        "white": {"username": white, "result": white_result},
        "black": {"username": black, "result": black_result},
    }


def stats_with(rapid=None, blitz=None, bullet=None, puzzle=None):
    s = {}
    if rapid is not None:
        s["chess_rapid"] = {"last": {"rating": rapid}}
    if blitz is not None:
        s["chess_blitz"] = {"last": {"rating": blitz}}
    if bullet is not None:
        s["chess_bullet"] = {"last": {"rating": bullet}}
    if puzzle is not None:
        s["tactics"] = {"highest": {"rating": puzzle}}
    return s


# --- helpers purs ------------------------------------------------------------

def test_week_start():
    assert lb._week_start(NOW) == dt(2026, 6, 29, 0)
    # lundi 00h00 pile : c'est déjà le début de semaine
    assert lb._week_start(dt(2026, 6, 29, 0)) == dt(2026, 6, 29, 0)
    # dimanche 23h59 : encore la semaine précédente
    assert lb._week_start(dt(2026, 6, 28, 23, 59)) == dt(2026, 6, 22, 0)


def test_player_side():
    g = game("Alice", "bob", "win", "resigned", 0)
    me, opp = lb._player_side(g, "alice")  # insensible à la casse
    assert me["username"] == "Alice"
    assert opp["username"] == "bob"
    me, opp = lb._player_side(g, "carol")
    assert me is None and opp is None


def test_result_kind():
    assert lb._result_kind("win") == "win"
    for r in ("agreed", "repetition", "stalemate", "insufficient",
              "50move", "timevsinsufficient"):
        assert lb._result_kind(r) == "draw", r
    for r in ("checkmated", "timeout", "resigned", "abandoned", "lose",
              "resultat_inconnu"):
        assert lb._result_kind(r) == "loss", r


def test_parse_time_classes():
    assert lb.parse_time_classes(None) is None
    assert lb.parse_time_classes("") is None
    assert lb.parse_time_classes("all") is None
    assert lb.parse_time_classes("rapid") == {"rapid"}
    assert lb.parse_time_classes("rapid, Blitz") == {"rapid", "blitz"}
    with pytest.raises(ValueError):
        lb.parse_time_classes("foo")


def test_should_post_recap():
    ws = lb._week_start(NOW)
    assert lb.should_post_recap(None, ws) is False        # premier lancement
    assert lb.should_post_recap("2026-06-29", ws) is False  # même semaine
    assert lb.should_post_recap("2026-06-22", ws) is True   # semaine passée


# --- gather ------------------------------------------------------------------

async def test_gather_counts_and_member_games():
    g_old = game("alice", "bob", "win", "checkmated", ts(2026, 6, 28), uuid="g0")
    g_win = game("alice", "bob", "win", "checkmated", ts(2026, 6, 30), uuid="g1")
    g_loss = game("stranger", "alice", "win", "resigned", ts(2026, 6, 30, 13), uuid="g2")
    g_draw = game("alice", "stranger", "agreed", "agreed", ts(2026, 7, 1, 9), uuid="g3")
    client = FakeClient(
        stats={"alice": stats_with(rapid=1500), "bob": stats_with(rapid=1400)},
        archives={
            ("alice", 2026, 6): {"games": [g_old, g_win, g_loss]},
            ("alice", 2026, 7): {"games": [g_draw]},
            ("bob", 2026, 6): {"games": [g_win]},  # la même partie, côté bob
        },
    )
    data, member_games = await lb.gather(client, ["alice", "bob"], now=NOW)

    a = data["alice"]
    assert (a["games_week"], a["wins_week"], a["losses_week"], a["draws_week"]) == (3, 1, 1, 1)
    assert a["wins_vs_members"] == 1  # g_old est hors semaine
    b = data["bob"]
    assert (b["games_week"], b["losses_week"]) == (1, 1)

    # semaine à cheval : les 2 archives (juin + juillet) sont interrogées
    assert ("archive", "alice", 2026, 6) in client.calls
    assert ("archive", "alice", 2026, 7) in client.calls

    # une seule entrée malgré la partie vue dans les archives des 2 joueurs
    assert [g["id"] for g in member_games] == ["g1"]
    assert member_games[0]["winner"] == "alice"
    assert member_games[0]["loser"] == "bob"
    assert member_games[0]["draw"] is False


async def test_gather_time_class_filter():
    g_rapid = game("alice", "x", "win", "resigned", ts(2026, 6, 30), uuid="r1")
    g_blitz = game("alice", "x", "win", "resigned", ts(2026, 6, 30, 13),
                   time_class="blitz", uuid="b1")
    client = FakeClient(
        stats={"alice": {}},
        archives={("alice", 2026, 6): {"games": [g_rapid, g_blitz]}},
    )
    data, _ = await lb.gather(client, ["alice"], time_classes={"rapid"}, now=NOW)
    assert data["alice"]["games_week"] == 1


async def test_gather_prev_week():
    prev = game("alice", "bob", "win", "timeout", ts(2026, 6, 24), uuid="p1")
    cur = game("alice", "bob", "win", "timeout", ts(2026, 6, 30), uuid="c1")
    client = FakeClient(
        stats={"alice": {}, "bob": {}},
        archives={("alice", 2026, 6): {"games": [prev, cur]}},
    )
    data, member_games = await lb.gather(client, ["alice", "bob"],
                                         prev_week=True, now=NOW)
    a = data["alice"]
    assert (a["games_week"], a["wins_week"]) == (1, 1)
    assert (a["prev_games_week"], a["prev_wins_week"], a["prev_wins_vs_members"]) == (1, 1, 1)
    # la partie de la semaine précédente n'est pas candidate aux annonces
    assert [g["id"] for g in member_games] == ["c1"]


async def test_gather_prev_week_adds_month():
    now = dt(2026, 6, 3)  # semaine du lundi 1er juin ; précédente : 25 mai
    client = FakeClient(stats={"alice": {}})
    await lb.gather(client, ["alice"], prev_week=True, now=now)
    assert ("archive", "alice", 2026, 5) in client.calls

    client2 = FakeClient(stats={"alice": {}})
    await lb.gather(client2, ["alice"], now=now)
    assert ("archive", "alice", 2026, 5) not in client2.calls


async def test_gather_skips_failing_player():
    client = FakeClient(stats={"bob": stats_with(rapid=1400)}, fail={"alice"})
    data, _ = await lb.gather(client, ["alice", "bob"], now=NOW)
    assert "alice" not in data
    assert data["bob"]["stats"] == stats_with(rapid=1400)


# --- boards ------------------------------------------------------------------

def _data(**per_user):
    return {u: d for u, d in per_user.items()}


def test_board_games():
    data = _data(
        a={"stats": {}, "games_week": 2, "wins_week": 1},
        b={"stats": {}, "games_week": 5, "wins_week": 5},
        c={"stats": {}, "games_week": 0, "wins_week": 0},
    )
    rows = lb.board_games(data)
    assert [r[0] for r in rows] == ["b", "a", "c"]
    assert rows[0][2] == 100.0
    assert rows[2][2] == 0.0  # pas de division par zéro


def test_board_elo():
    data = _data(
        a={"stats": stats_with(rapid=1200)},
        b={"stats": stats_with(rapid=1600)},
        c={"stats": {}},  # pas d'Elo rapide -> exclu
    )
    assert lb.board_elo(data) == [("b", 1600), ("a", 1200)]


def test_board_wins():
    data = _data(
        a={"stats": {}, "wins_vs_members": 1},
        b={"stats": {}, "wins_vs_members": 3},
    )
    assert lb.board_wins(data) == [("b", 3), ("a", 1)]


def test_board_progress():
    data = _data(
        a={"stats": stats_with(rapid=1250)},
        b={"stats": stats_with(rapid=1580)},
        c={"stats": stats_with(rapid=1000)},  # pas de baseline -> exclu
        d={"stats": {}},                      # pas d'Elo -> exclu
    )
    baselines = {
        "a": {"rapid": 1200},
        "b": {"rapid": 1600},
        "d": {"rapid": 900},
    }
    assert lb.board_progress(data, baselines) == [("a", 1250, 50), ("b", 1580, -20)]


# --- head_to_head ------------------------------------------------------------

async def test_head_to_head():
    g_win = game("alice", "bob", "win", "resigned", ts(2026, 2, 10), uuid="h1")
    g_loss = game("bob", "alice", "win", "timeout", ts(2026, 5, 10), uuid="h2")
    g_draw = game("alice", "bob", "stalemate", "stalemate", ts(2026, 6, 10), uuid="h3")
    g_other = game("alice", "stranger", "win", "resigned", ts(2026, 6, 11), uuid="h4")
    client = FakeClient(archives={
        ("alice", 2026, 2): {"games": [g_win]},
        ("alice", 2026, 5): {"games": [g_loss]},
        ("alice", 2026, 6): {"games": [g_draw, g_other]},
    })
    h = await lb.head_to_head(client, "alice", "bob", months=6, now=NOW)
    assert (h["wins"], h["losses"], h["draws"], h["games"]) == (1, 1, 1, 3)
    assert h["last_url"].endswith("h3")
    # seules les archives de l'invocateur sont interrogées, une par mois
    archive_calls = [c for c in client.calls if c[0] == "archive"]
    assert len(archive_calls) == 6
    assert all(c[1] == "alice" for c in archive_calls)
