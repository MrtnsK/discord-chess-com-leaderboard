"""Bot Discord de classements chess.com.

Commandes :
  /join <pseudo>   inscrit ton compte chess.com
  /leave           te retire des classements
  /members         liste les inscrits
  /setup <salon>   (admin) choisit le salon et poste les 3 messages
  /refresh         (admin) force une mise à jour

Mise à jour auto à 00h / 06h / 12h / 18h (heure locale TZ_NAME).
"""

import os
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
import leaderboards as lb
from chesscom import ChessComClient

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
REFRESH_HOURS = (0, 6, 12, 18)
REFRESH_TIMES = [datetime.time(hour=h, tzinfo=lb.TZ) for h in REFRESH_HOURS]

MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


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


# --- coeur : récupère les données et édite les 3 messages -------------------

async def do_refresh():
    channel_id = db.get_config("channel_id")
    if not channel_id:
        return
    players = db.list_players()
    if not players:
        return

    players_map = {p["chesscom_username"]: p["discord_id"] for p in players}
    usernames = list(players_map.keys())

    async with ChessComClient() as client:
        data = await lb.gather(client, usernames)

    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))

    boards = [
        ("msg_games", build_embed(
            "🎮 Parties jouées cette semaine",
            lb.board_games(data), players_map, 0x57F287,
            lambda r: f"**{r[1]}** parties · {r[2]:.0f}% V")),
        ("msg_elo", build_embed(
            "⚡ Classement Elo rapide",
            lb.board_elo(data), players_map, 0x5865F2,
            lambda r: f"**{r[1]}**")),
        ("msg_wins", build_embed(
            "🏆 Victoires de la semaine (entre membres)",
            lb.board_wins(data), players_map, 0xFEE75C,
            lambda r: f"**{r[1]}** V")),
    ]

    for key, embed in boards:
        mid = db.get_config(key)
        if not mid:
            continue
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.edit(embed=embed)
        except discord.NotFound:
            sent = await channel.send(embed=embed)
            db.set_config(key, sent.id)


# --- scheduler -------------------------------------------------------------

@tasks.loop(time=REFRESH_TIMES)
async def scheduled_refresh():
    await do_refresh()


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

    async with ChessComClient() as client:
        if not await client.player_exists(pseudo):
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


@setup.error
@refresh.error
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
    bot.run(TOKEN)
