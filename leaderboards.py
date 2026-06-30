"""Calcul des 3 classements à partir des données chess.com.

On récupère les données UNE fois par joueur (1 appel /stats + 1-2 appels
d'archives), puis on construit les 3 boards à partir de ça.
"""

import os
import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))

# Time classes comptées pour les parties de la semaine (boards 1 et 3).
# None = toutes les parties. Ex: {"rapid"} pour ne compter que le rapide.
GAMES_TIME_CLASSES = None


def _week_start():
    """Lundi 00:00 de la semaine en cours (heure locale)."""
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


async def gather(client, usernames):
    """Retourne {username: {stats, games_week, wins_week, wins_vs_members}}.

    Tout est calculé sur la semaine en cours, en un seul passage sur les parties.
    Appels en série (contrainte de l'API chess.com).
    """
    result = {}
    members = {u.lower() for u in usernames}  # qui est "inscrit"
    week_start = _week_start()
    week_start_ts = week_start.timestamp()
    now = datetime.datetime.now(TZ)

    # Mois à interroger : celui du début de semaine + le mois courant
    # (gère le cas où la semaine est à cheval sur 2 mois).
    months = {(week_start.year, week_start.month), (now.year, now.month)}

    for username in usernames:
        stats = await client.get_stats(username)

        games_week = wins_week = wins_vs_members = 0
        for (year, month) in months:
            archive = await client.get_archive(username, year, month)
            if not archive:
                continue
            for g in archive.get("games", []):
                if g.get("end_time", 0) < week_start_ts:
                    continue
                if GAMES_TIME_CLASSES and g.get("time_class") not in GAMES_TIME_CLASSES:
                    continue

                games_week += 1
                me, opp = _player_side(g, username)
                if me and me.get("result") == "win":
                    wins_week += 1
                    if opp and opp.get("username", "").lower() in members:
                        wins_vs_members += 1

        result[username] = {
            "stats": stats,
            "games_week": games_week,
            "wins_week": wins_week,
            "wins_vs_members": wins_vs_members,
        }

    return result


# --- extraction de valeurs -------------------------------------------------

def _rapid_rating(stats):
    try:
        return stats["chess_rapid"]["last"]["rating"]
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
    rows = [(u, _rapid_rating(d["stats"])) for u, d in data.items()]
    rows = [r for r in rows if r[1] is not None]
    return sorted(rows, key=lambda x: x[1], reverse=True)


def board_wins(data):
    """(username, victoires_de_la_semaine_contre_membres) trié décroissant."""
    rows = [(u, d["wins_vs_members"]) for u, d in data.items()]
    return sorted(rows, key=lambda x: x[1], reverse=True)
