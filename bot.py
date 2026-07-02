"""Bot Discord de classements chess.com.

Commandes :
  /join <pseudo>        inscrit ton compte chess.com
  /leave                te retire des classements
  /members              liste les inscrits
  /stats [membre]       carte de stats d'un membre
  /vs <adversaire>      ton bilan contre un autre membre
  /setup <salon>        (admin) choisit le salon et poste les classements
  /refresh              (admin) force une mise à jour
  /config timeclass     (admin) filtre les types de parties comptées

Mise à jour auto à 00h / 06h / 12h / 18h (heure locale TZ_NAME).
"""

import os
import asyncio
import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
import leaderboards as lb
from chesscom import ChessComClient

load_dotenv()

log = logging.getLogger("chessbot")

TOKEN = os.environ["DISCORD_TOKEN"]
REFRESH_HOURS = (0, 6, 12, 18)
REFRESH_TIMES = [datetime.time(hour=h, tzinfo=lb.TZ) for h in REFRESH_HOURS]
VS_MONTHS = int(os.environ.get("VS_MONTHS", "6"))
MAX_ANNOUNCES = 8  # au-delà, un seul message groupé au lieu d'un par partie

MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}
TIME_CLASS_FR = {"rapid": "rapide", "blitz": "blitz", "bullet": "bullet", "daily": "daily"}

# L'API chess.com refuse les requêtes parallèles : un seul client actif à la
# fois (un /vs pendant le refresh planifié = 429).
API_LOCK = asyncio.Lock()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def current_time_classes():
    """Filtre des types de parties : config DB, sinon env, sinon toutes."""
    raw = db.get_config("time_classes") or os.environ.get("GAMES_TIME_CLASSES")
    try:
        return lb.parse_time_classes(raw)
    except ValueError:
        log.warning("Filtre time_classes invalide (%r), ignoré", raw)
        return None


def _fmt_opt(value):
    return str(value) if value is not None else "—"


# --- rendu d'un classement en embed ----------------------------------------

def build_embed(title, rows, players_map, color, fmt):
    lines = []
    for i, row in enumerate(rows[:25]):
        username = row[0]
        rank = MEDALS.get(i, f"`{i + 1}.`")
        did = players_map.get(username)
        name = f"<@{did}>" if did else username
        lines.append(f"{rank} {name} — {fmt(row)}")
    if not lines:
        lines = ["*Aucun inscrit pour le moment. Tapez `/join`.*"]
    embed = discord.Embed(title=title, description="\n".join(lines), color=color)
    embed.timestamp = datetime.datetime.now(tz=lb.TZ)
    embed.set_footer(text="Dernière maj")
    return embed


# --- récap hebdo -------------------------------------------------------------

def build_recap_embed(data, players_map, week_start):
    """Bilan figé de la semaine écoulée (compteurs prev_* de gather)."""
    prev_start = week_start - datetime.timedelta(days=7)

    def mention(username):
        did = players_map.get(username)
        return f"<@{did}>" if did else username

    active = [(u, d) for u, d in data.items() if d["prev_games_week"] > 0]
    winners = sorted(
        [(u, d) for u, d in active if d["prev_wins_vs_members"] > 0],
        key=lambda kv: (
            kv[1]["prev_wins_vs_members"],
            kv[1]["prev_wins_week"],
            kv[1]["prev_wins_week"] / kv[1]["prev_games_week"],
        ),
        reverse=True,
    )

    if not active:
        desc = "Semaine calme, personne n'a joué 😴"
    elif winners:
        lines = [
            f"{MEDALS[i]} {mention(u)} — **{d['prev_wins_vs_members']}** V entre membres"
            f" ({d['prev_wins_week']} V au total)"
            for i, (u, d) in enumerate(winners[:3])
        ]
        desc = "\n".join(lines)
    else:
        # aucune victoire entre membres : on couronne les plus actifs
        top = sorted(active, key=lambda kv: kv[1]["prev_games_week"], reverse=True)
        lines = [
            f"{MEDALS[i]} {mention(u)} — **{d['prev_games_week']}** parties"
            for i, (u, d) in enumerate(top[:3])
        ]
        desc = "Aucune victoire entre membres — place aux plus actifs :\n" + "\n".join(lines)

    return discord.Embed(
        title=f"👑 Vainqueur de la semaine du {prev_start:%d/%m}",
        description=desc,
        color=0xF1C40F,
    )


# --- annonces des parties entre membres --------------------------------------

async def announce_new_games(channel, member_games, players_map):
    """Annonce les nouvelles parties entre membres (dédup persistante)."""
    if not member_games:
        return

    # Premier lancement : tout marquer comme annoncé SANS poster, sinon on
    # spammerait tout l'historique de la semaine d'un coup.
    if db.get_config("announce_bootstrap") is None:
        db.mark_games_announced([g["id"] for g in member_games])
        db.set_config("announce_bootstrap", "done")
        return

    new = [g for g in member_games if not db.is_game_announced(g["id"])]
    new.sort(key=lambda g: g["end_time"])

    # les nuls ne sont pas annoncés, mais marqués pour ne pas les re-scanner
    draws = [g for g in new if g["draw"]]
    if draws:
        db.mark_games_announced([g["id"] for g in draws])
    decisive = [g for g in new if not g["draw"]]
    if not decisive:
        return

    def mention(username):
        did = players_map.get(username)
        return f"<@{did}>" if did else username

    def line(g):
        tc = TIME_CLASS_FR.get(g["time_class"], g["time_class"])
        return (f"⚔️ {mention(g['winner'])} a battu {mention(g['loser'])} !"
                f" ([{tc}](<{g['url']}>))")

    try:
        if len(decisive) > MAX_ANNOUNCES:
            # message groupé, découpé sous la limite Discord (2000 caractères)
            content = "**⚔️ Ça a joué entre membres !**"
            for g in decisive:
                l = line(g)
                if len(content) + 1 + len(l) > 1900:
                    await channel.send(content)
                    content = l
                else:
                    content += "\n" + l
            await channel.send(content)
            db.mark_games_announced([g["id"] for g in decisive])
        else:
            # marqué après CHAQUE envoi réussi : en cas d'échec au milieu,
            # seules les parties non envoyées seront retentées
            for g in decisive:
                await channel.send(line(g))
                db.mark_games_announced([g["id"]])
    except discord.HTTPException:
        log.exception("Échec d'annonce des parties entre membres")


# --- coeur : récupère les données et édite les messages ----------------------

async def do_refresh():
    channel_id = db.get_config("channel_id")
    if not channel_id:
        return
    players = db.list_players()
    if not players:
        return

    players_map = {p["chesscom_username"]: p["discord_id"] for p in players}
    usernames = list(players_map.keys())

    week_start = lb._week_start()
    week_iso = week_start.date().isoformat()
    last_recap = db.get_config("last_recap_week")
    post_recap = lb.should_post_recap(last_recap, week_start)

    async with API_LOCK:
        async with ChessComClient(cache=db.SqliteHttpCache()) as client:
            data, member_games = await lb.gather(
                client, usernames,
                time_classes=current_time_classes(),
                prev_week=post_recap,
            )

    if not data:
        log.warning("Aucune donnée récupérée, refresh abandonné")
        return

    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))

    # récap de la semaine écoulée, posté avant l'édition des boards
    if last_recap is None:
        db.set_config("last_recap_week", week_iso)  # premier lancement, rien à poster
    elif post_recap:
        await channel.send(embed=build_recap_embed(data, players_map, week_start))
        db.set_config("last_recap_week", week_iso)

    # snapshot des ratings (baselines des deltas Elo)
    taken_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for u, d in data.items():
        db.save_rating_snapshot(
            u,
            lb.rating_of(d["stats"], "chess_rapid"),
            lb.rating_of(d["stats"], "chess_blitz"),
            lb.rating_of(d["stats"], "chess_bullet"),
            taken_at,
        )
    week_start_utc = week_start.astimezone(datetime.timezone.utc).isoformat()
    baselines = {u: db.get_baseline_ratings(u, week_start_utc) for u in data}
    progress = lb.board_progress(data, baselines)
    deltas = {u: delta for u, _, delta in progress}

    def fmt_elo(r):
        delta = deltas.get(r[0])
        return f"**{r[1]}**" if delta is None else f"**{r[1]}** ({delta:+d})"

    boards = [
        ("msg_games", build_embed(
            "🎮 Parties jouées cette semaine",
            lb.board_games(data), players_map, 0x57F287,
            lambda r: f"**{r[1]}** parties · {r[2]:.0f}% V")),
        ("msg_elo", build_embed(
            "⚡ Classement Elo rapide",
            lb.board_elo(data), players_map, 0x5865F2, fmt_elo)),
        ("msg_wins", build_embed(
            "🏆 Victoires de la semaine (entre membres)",
            lb.board_wins(data), players_map, 0xFEE75C,
            lambda r: f"**{r[1]}** V")),
        ("msg_progress", build_embed(
            "📈 Progression de la semaine",
            progress, players_map, 0xEB459E,
            lambda r: f"**{r[2]:+d}** ({r[1]})")),
    ]

    for key, embed in boards:
        try:
            msg = None
            mid = db.get_config(key)
            if mid:
                try:
                    msg = await channel.fetch_message(int(mid))
                except discord.NotFound:
                    msg = None
            if msg:
                await msg.edit(embed=embed)
            else:
                # message supprimé OU nouveau board (migration) : on le (re)crée
                sent = await channel.send(embed=embed)
                db.set_config(key, sent.id)
        except discord.HTTPException:
            log.exception("Échec de mise à jour du board %s", key)

    await announce_new_games(channel, member_games, players_map)

    db.purge_rating_history()
    db.cache_purge()


# --- scheduler -------------------------------------------------------------

@tasks.loop(time=REFRESH_TIMES)
async def scheduled_refresh():
    try:
        await do_refresh()
    except Exception:
        # sans ça, la première exception arrête définitivement la loop
        log.exception("Échec du refresh planifié")


@scheduled_refresh.before_loop
async def _before():
    await bot.wait_until_ready()


# --- cycle de vie ----------------------------------------------------------

@bot.event
async def setup_hook():
    db.init()
    guild_id = os.environ.get("GUILD_ID")
    if guild_id:  # sync instantané sur un serveur précis (pratique pour tester)
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:  # sync global (peut prendre jusqu'à 1h à se propager)
        await bot.tree.sync()
    scheduled_refresh.start()


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} (id: {bot.user.id})")


# --- commandes -------------------------------------------------------------

@bot.tree.command(description="Rejoindre les classements avec ton pseudo Chess.com")
@app_commands.describe(pseudo="Ton nom d'utilisateur Chess.com")
async def join(interaction: discord.Interaction, pseudo: str):
    await interaction.response.defer(ephemeral=True)
    pseudo = pseudo.strip().lower()

    existing = db.get_by_username(pseudo)
    if existing and existing["discord_id"] != interaction.user.id:
        await interaction.followup.send(
            "Ce pseudo Chess.com est déjà utilisé par un autre membre.", ephemeral=True
        )
        return

    async with API_LOCK:
        async with ChessComClient() as client:
            exists = await client.player_exists(pseudo)
    if not exists:
        await interaction.followup.send(
            f"Le compte Chess.com `{pseudo}` est introuvable.", ephemeral=True
        )
        return

    db.add_player(interaction.user.id, pseudo)
    await interaction.followup.send(f"Inscrit en tant que `{pseudo}` ✅", ephemeral=True)


@bot.tree.command(description="Te retirer des classements")
async def leave(interaction: discord.Interaction):
    ok = db.remove_player(interaction.user.id)
    msg = "Tu as quitté les classements." if ok else "Tu n'étais pas inscrit."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Voir les membres inscrits")
async def members(interaction: discord.Interaction):
    players = db.list_players()
    if not players:
        await interaction.response.send_message("Aucun inscrit.", ephemeral=True)
        return
    lines = [f"<@{p['discord_id']}> → `{p['chesscom_username']}`" for p in players]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(description="Carte de stats chess.com d'un membre")
@app_commands.describe(membre="Le membre à afficher (toi par défaut)")
async def stats(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    target = membre or interaction.user
    player = db.get_by_discord_id(target.id)
    if not player:
        await interaction.response.send_message(
            f"{target.display_name} n'est pas inscrit (`/join`).", ephemeral=True
        )
        return
    await interaction.response.defer()

    username = player["chesscom_username"]
    all_usernames = [p["chesscom_username"] for p in db.list_players()]
    async with API_LOCK:
        async with ChessComClient(cache=db.SqliteHttpCache()) as client:
            data, _ = await lb.gather(
                client, [username],
                members=all_usernames,
                time_classes=current_time_classes(),
            )

    d = data.get(username)
    if not d:
        await interaction.followup.send("Impossible de récupérer les stats, réessaie plus tard.")
        return

    s = d["stats"]
    rapid = lb.rating_of(s, "chess_rapid")
    week_start_utc = lb._week_start().astimezone(datetime.timezone.utc).isoformat()
    base = db.get_baseline_ratings(username, week_start_utc)
    delta = None
    if rapid is not None and base and base.get("rapid") is not None:
        delta = rapid - base["rapid"]

    embed = discord.Embed(
        title=f"♟️ Stats de {username}",
        url=f"https://www.chess.com/member/{username}",
        color=0x5865F2,
    )
    rapid_txt = _fmt_opt(rapid)
    if delta is not None:
        rapid_txt += f" ({delta:+d} cette semaine)"
    embed.add_field(name="⚡ Rapide", value=rapid_txt)
    embed.add_field(name="🔥 Blitz", value=_fmt_opt(lb.rating_of(s, "chess_blitz")))
    embed.add_field(name="🚀 Bullet", value=_fmt_opt(lb.rating_of(s, "chess_bullet")))
    embed.add_field(name="🧩 Puzzles", value=_fmt_opt(lb.puzzle_rating(s)))
    embed.add_field(
        name="📅 Cette semaine",
        value=f"{d['wins_week']} V / {d['draws_week']} N / {d['losses_week']} D"
              f" ({d['games_week']} parties)",
    )
    embed.add_field(name="🏆 V entre membres", value=str(d["wins_vs_members"]))
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(description="Ton bilan contre un autre membre")
@app_commands.describe(adversaire="Le membre à comparer")
async def vs(interaction: discord.Interaction, adversaire: discord.Member):
    if adversaire.id == interaction.user.id:
        await interaction.response.send_message(
            "Tu ne peux pas t'affronter toi-même 🙃", ephemeral=True
        )
        return
    me = db.get_by_discord_id(interaction.user.id)
    opp = db.get_by_discord_id(adversaire.id)
    if not me or not opp:
        who = "Tu n'es pas inscrit" if not me else f"{adversaire.display_name} n'est pas inscrit"
        await interaction.response.send_message(f"{who} (`/join`).", ephemeral=True)
        return
    await interaction.response.defer()

    async with API_LOCK:
        async with ChessComClient(cache=db.SqliteHttpCache()) as client:
            h2h = await lb.head_to_head(
                client, me["chesscom_username"], opp["chesscom_username"],
                months=VS_MONTHS,
            )

    if h2h["games"] == 0:
        await interaction.followup.send(
            f"Aucune partie entre vous sur les {VS_MONTHS} derniers mois."
        )
        return

    desc = (f"Sur les {VS_MONTHS} derniers mois ({h2h['games']} parties) :\n"
            f"**{h2h['wins']} V / {h2h['draws']} N / {h2h['losses']} D**"
            f" pour {interaction.user.mention}")
    if h2h["last_url"]:
        desc += f"\n[Dernière partie]({h2h['last_url']})"
    embed = discord.Embed(
        title=f"⚔️ {me['chesscom_username']} vs {opp['chesscom_username']}",
        description=desc,
        color=0xED4245,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(description="(Admin) Choisir le salon des classements")
@app_commands.describe(salon="Le salon où afficher les classements")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    db.set_config("channel_id", salon.id)
    titles = [
        ("msg_games", "🎮 Parties jouées cette semaine"),
        ("msg_elo", "⚡ Classement Elo rapide"),
        ("msg_wins", "🏆 Victoires de la semaine (entre membres)"),
        ("msg_progress", "📈 Progression de la semaine"),
    ]
    for key, title in titles:
        sent = await salon.send(embed=discord.Embed(title=title, description="Initialisation…"))
        db.set_config(key, sent.id)
    await do_refresh()
    await interaction.followup.send(f"Classements configurés dans {salon.mention} ✅", ephemeral=True)


@bot.tree.command(description="(Admin) Forcer la mise à jour des classements")
@app_commands.checks.has_permissions(administrator=True)
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await do_refresh()
    await interaction.followup.send("Classements mis à jour ✅", ephemeral=True)


config_group = app_commands.Group(name="config", description="(Admin) Réglages du bot")


@config_group.command(
    name="timeclass",
    description="(Admin) Types de parties comptées dans les classements",
)
@app_commands.describe(valeurs='Ex : "rapid", "rapid,blitz" ou "all" pour tout compter')
@app_commands.checks.has_permissions(administrator=True)
async def config_timeclass(interaction: discord.Interaction, valeurs: str):
    try:
        parsed = lb.parse_time_classes(valeurs)
    except ValueError as e:
        await interaction.response.send_message(
            f"Valeurs inconnues : {e}. Choix : {', '.join(sorted(lb.TIME_CLASSES))} ou `all`.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    db.set_config("time_classes", "all" if parsed is None else ",".join(sorted(parsed)))
    await do_refresh()
    label = "toutes les parties" if parsed is None else ", ".join(sorted(parsed))
    await interaction.followup.send(f"Parties comptées : {label} ✅", ephemeral=True)


bot.tree.add_command(config_group)


@setup.error
@refresh.error
@config_timeclass.error
async def _admin_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Il faut être administrateur pour cette commande."
    else:
        msg = f"Erreur : {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot.run(TOKEN)
