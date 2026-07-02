"""Calcul des classements à partir des données chess.com.

On récupère les données UNE fois par joueur (1 appel /stats + 1-2 appels
d'archives), puis on construit les boards à partir de ça.
"""

import os
import logging
import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))

log = logging.getLogger("chessbot")

TIME_CLASSES = {"rapid", "blitz", "bullet", "daily"}

# Résultats chess.com (champ "result") regroupés en nuls ; tout ce qui n'est
# ni "win" ni un nul est compté comme défaite.
_DRAW_RESULTS = {
    "agreed", "repetition", "stalemate",
    "insufficient", "50move", "timevsinsufficient",
}


def parse_time_classes(raw):
    """CSV -> set de time classes, None = toutes ("all"/vide).

    Lève ValueError sur une valeur inconnue.
    """
    if raw is None:
        return None
    values = {v.strip().lower() for v in raw.split(",") if v.strip()}
    if not values or values == {"all"}:
        return None
    invalid = values - TIME_CLASSES
    if invalid:
        raise ValueError(", ".join(sorted(invalid)))
    return values


def _week_start(now=None):
    """Lundi 00:00 de la semaine en cours (heure locale)."""
    if now is None:
        now = datetime.datetime.now(TZ)
    start = now - datetime.timedelta(days=now.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def _player_side(game, username):
    """Retourne (mon_camp, adversaire) pour ce joueur dans la partie, ou (None, None)."""
    u = username.lower()
    white = game.get("white", {})
    black = game.get("black", {})
    if white.get("username", "").lower() == u:
        return white, black
    if black.get("username", "").lower() == u:
        return black, white
    return None, None


def _result_kind(result):
    """Classe un résultat chess.com en "win" / "draw" / "loss"."""
    if result == "win":
        return "win"
    if result in _DRAW_RESULTS:
        return "draw"
    return "loss"


async def gather(client, usernames, *, members=None, time_classes=None,
                 prev_week=False, now=None):
    """Retourne (data, member_games).

    data : {username: {stats, games_week, wins_week, losses_week, draws_week,
                       wins_vs_members [, prev_games_week, prev_wins_week,
                       prev_wins_vs_members si prev_week]}}
    member_games : parties de la semaine entre membres (dédupliquées — chaque
    partie apparaît dans les archives des DEUX joueurs), pour les annonces.

    - members : périmètre des "inscrits" (défaut : usernames) — permet de
      traiter un seul joueur avec le bon périmètre (/stats).
    - prev_week : calcule aussi les compteurs de la semaine précédente
      (récap hebdo), toujours en un seul passage sur les parties.
    - now : injectable pour les tests.

    Appels en série (contrainte de l'API chess.com). Un joueur en erreur est
    ignoré pour ce cycle (log) au lieu de faire échouer tout le refresh.
    """
    result = {}
    members = {u.lower() for u in (members if members is not None else usernames)}
    if now is None:
        now = datetime.datetime.now(TZ)
    week_start = _week_start(now)
    week_start_ts = week_start.timestamp()
    prev_start = week_start - datetime.timedelta(days=7)
    prev_start_ts = prev_start.timestamp()
    since_ts = prev_start_ts if prev_week else week_start_ts

    # Mois à interroger : début de fenêtre + mois courant (gère les semaines
    # à cheval sur 2 mois).
    months = {(week_start.year, week_start.month), (now.year, now.month)}
    if prev_week:
        months.add((prev_start.year, prev_start.month))

    member_games = []
    seen_games = set()

    for username in usernames:
        try:
            stats = await client.get_stats(username)

            counters = dict(games_week=0, wins_week=0, losses_week=0,
                            draws_week=0, wins_vs_members=0)
            if prev_week:
                counters.update(prev_games_week=0, prev_wins_week=0,
                                prev_wins_vs_members=0)

            for (year, month) in sorted(months):
                archive = await client.get_archive(username, year, month)
                if not archive:
                    continue
                for g in archive.get("games", []):
                    end = g.get("end_time", 0)
                    if end < since_ts:
                        continue
                    if time_classes and g.get("time_class") not in time_classes:
                        continue
                    me, opp = _player_side(g, username)
                    if me is None:
                        continue
                    kind = _result_kind(me.get("result"))
                    opp_name = opp.get("username", "").lower()
                    opp_is_member = opp_name in members

                    if end >= week_start_ts:  # semaine courante
                        counters["games_week"] += 1
                        if kind == "win":
                            counters["wins_week"] += 1
                            if opp_is_member:
                                counters["wins_vs_members"] += 1
                        elif kind == "loss":
                            counters["losses_week"] += 1
                        else:
                            counters["draws_week"] += 1

                        if opp_is_member:
                            gid = g.get("uuid") or g.get("url")
                            if gid and gid not in seen_games:
                                seen_games.add(gid)
                                me_name = me.get("username", "").lower()
                                member_games.append({
                                    "id": gid,
                                    "url": g.get("url"),
                                    "end_time": end,
                                    "time_class": g.get("time_class"),
                                    "draw": kind == "draw",
                                    "winner": me_name if kind == "win" else opp_name,
                                    "loser": opp_name if kind == "win" else me_name,
                                })
                    else:  # semaine précédente (prev_week uniquement)
                        counters["prev_games_week"] += 1
                        if kind == "win":
                            counters["prev_wins_week"] += 1
                            if opp_is_member:
                                counters["prev_wins_vs_members"] += 1
        except Exception:
            log.exception("Joueur %s ignoré pour ce cycle", username)
            continue

        result[username] = {"stats": stats, **counters}

    return result, member_games


async def head_to_head(client, username_a, username_b, months=6, now=None):
    """Bilan de username_a contre username_b sur les N derniers mois.

    N'interroge que les archives de username_a (elles contiennent forcément
    toutes ses parties contre l'autre). Point de vue de username_a.
    """
    if now is None:
        now = datetime.datetime.now(TZ)
    b = username_b.lower()
    wins = losses = draws = 0
    last_url, last_end = None, 0

    pairs = []
    year, month = now.year, now.month
    for _ in range(months):
        pairs.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12

    for (y, m) in reversed(pairs):
        archive = await client.get_archive(username_a, y, m)
        if not archive:
            continue
        for g in archive.get("games", []):
            me, opp = _player_side(g, username_a)
            if me is None or opp.get("username", "").lower() != b:
                continue
            kind = _result_kind(me.get("result"))
            if kind == "win":
                wins += 1
            elif kind == "loss":
                losses += 1
            else:
                draws += 1
            if g.get("end_time", 0) >= last_end:
                last_end = g.get("end_time", 0)
                last_url = g.get("url")

    return {"wins": wins, "losses": losses, "draws": draws,
            "games": wins + losses + draws, "last_url": last_url}


# --- extraction de valeurs -------------------------------------------------

def rating_of(stats, category):
    """Rating "last" d'une catégorie ("chess_rapid", "chess_blitz", ...)."""
    try:
        return stats[category]["last"]["rating"]
    except (KeyError, TypeError):
        return None


def puzzle_rating(stats):
    # La PubAPI n'expose que highest/lowest pour les puzzles, jamais le
    # rating courant — on affiche donc le record (libellé "highest").
    try:
        return stats["tactics"]["highest"]["rating"]
    except (KeyError, TypeError):
        return None


# --- construction des classements (listes triées) --------------------------

def board_games(data):
    """(username, parties, pourcentage_victoires) trié par nombre de parties."""
    rows = []
    for u, d in data.items():
        games = d["games_week"]
        pct = (d["wins_week"] / games * 100) if games else 0.0
        rows.append((u, games, pct))
    return sorted(rows, key=lambda x: x[1], reverse=True)


def board_elo(data):
    rows = [(u, rating_of(d["stats"], "chess_rapid")) for u, d in data.items()]
    rows = [r for r in rows if r[1] is not None]
    return sorted(rows, key=lambda x: x[1], reverse=True)


def board_wins(data):
    """(username, victoires_de_la_semaine_contre_membres) trié décroissant."""
    rows = [(u, d["wins_vs_members"]) for u, d in data.items()]
    return sorted(rows, key=lambda x: x[1], reverse=True)


def board_progress(data, baselines):
    """(username, elo_rapide, delta_semaine) trié par delta décroissant.

    Les joueurs sans baseline ou sans Elo rapide sont exclus.
    """
    rows = []
    for u, d in data.items():
        current = rating_of(d["stats"], "chess_rapid")
        base = baselines.get(u)
        if current is None or not base or base.get("rapid") is None:
            continue
        rows.append((u, current, current - base["rapid"]))
    return sorted(rows, key=lambda x: x[2], reverse=True)


# --- récap hebdo -------------------------------------------------------------

def should_post_recap(last_recap_week, week_start):
    """True si on a changé de semaine depuis le dernier récap.

    last_recap_week est la date ISO du lundi du dernier récap (clé config) ;
    None = premier lancement, on initialise sans poster.
    """
    if last_recap_week is None:
        return False
    return last_recap_week != week_start.date().isoformat()
