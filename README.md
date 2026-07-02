# Chess.com Leaderboard Bot

Bot Discord qui affiche 4 classements dynamiques à partir de la PubAPI chess.com,
mis à jour automatiquement à **00h / 06h / 12h / 18h** :

1. 🎮 **Parties jouées cette semaine** (+ % de victoires sur la période)
2. ⚡ **Elo rapide** (`chess_rapid`, avec le delta de la semaine)
3. 🏆 **Victoires de la semaine** comptées uniquement contre d'autres membres inscrits
4. 📈 **Progression Elo de la semaine**

En plus des classements (qui **éditent le même message** à chaque maj, pas de
spam), le bot poste dans le même salon :

- un **récap hebdo** figé chaque lundi (👑 vainqueur de la semaine écoulée) ;
- une **annonce** pour chaque nouvelle partie jouée **entre membres inscrits**
  (⚔️ X a battu Y), sans doublon.

Seuls les membres inscrits via `/join` apparaissent.

## 1. Créer l'application Discord

1. https://discord.com/developers/applications → **New Application**
2. Onglet **Bot** → récupère le **Token** (→ `DISCORD_TOKEN`)
3. Onglet **Installation** / **OAuth2** → scopes `bot` + `applications.commands`,
   permissions au minimum : *Send Messages*, *Embed Links*. Invite le bot.
4. Aucun intent privilégié nécessaire (on n'utilise que des slash commands).

## 2. Installation locale

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis colle ton DISCORD_TOKEN
python bot.py
```

⚠️ Renseigne ton contact pour l'API chess.com, soit via la variable
`CHESSCOM_CONTACT` (recommandé), soit en éditant `USER_AGENT` dans `chesscom.py`.

## 3. Utilisation

| Commande                   | Qui    | Effet                                              |
|----------------------------|--------|----------------------------------------------------|
| `/join <pseudo>`           | tous   | s'inscrire avec son pseudo Chess.com               |
| `/leave`                   | tous   | se retirer                                         |
| `/members`                 | tous   | liste des inscrits                                 |
| `/stats [membre]`          | tous   | carte de stats (Elo, bilan de la semaine, puzzles) |
| `/vs <adversaire>`         | tous   | bilan face à un autre membre (6 derniers mois)     |
| `/setup <salon>`           | admin  | choisit le salon et poste les 4 classements        |
| `/refresh`                 | admin  | force une mise à jour immédiate                    |
| `/config timeclass <val>`  | admin  | types de parties comptées (`rapid,blitz`… ou `all`)|

Ordre typique : `/setup #classements`, puis chacun fait `/join`.

## 4. Hébergement

### VPS (systemd)

`/etc/systemd/system/chessbot.service` :

```ini
[Unit]
Description=Chess Leaderboard Bot
After=network.target

[Service]
WorkingDirectory=/opt/chess-leaderboard-bot
ExecStart=/opt/chess-leaderboard-bot/venv/bin/python bot.py
EnvironmentFile=/opt/chess-leaderboard-bot/.env
Restart=always
User=chessbot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now chessbot
journalctl -u chessbot -f   # voir les logs
```

### Pelican (pelican.dev) — egg sur-mesure

Un egg prêt à l'emploi est fourni : **`egg-chess-leaderboard.json`**.

1. Dans le panel Pelican : **Admin → Eggs → Import Egg**, choisis
   `egg-chess-leaderboard.json`.
2. Crée un serveur avec cet egg. Aucune allocation de port n'est utilisée par le
   bot (uniquement des appels sortants), tu peux laisser le port par défaut.
3. À l'installation, l'egg **clone ce repo** puis, à chaque démarrage, installe
   `requirements.txt` et lance `bot.py`. Tout est configurable depuis l'onglet
   **Startup** du serveur :

   | Variable            | Rôle                                                    |
   |---------------------|---------------------------------------------------------|
   | `DISCORD_TOKEN`     | **obligatoire** — token du bot                          |
   | `GIT_ADDRESS`       | URL du repo à cloner                                     |
   | `BRANCH`            | branche à cloner (défaut `main`)                        |
   | `GIT_USERNAME` / `GIT_ACCESS_TOKEN` | identifiants si le repo est privé       |
   | `AUTO_UPDATE`       | `1` = `git pull` à chaque démarrage                     |
   | `PY_FILE`           | fichier lancé (défaut `bot.py`)                         |
   | `REQUIREMENTS_FILE` | dépendances pip (défaut `requirements.txt`)            |
   | `GUILD_ID`          | sync instantané des commandes (test)                    |
   | `TZ_NAME`           | fuseau horaire des maj (défaut `Europe/Paris`)         |
   | `DB_PATH`           | base SQLite (défaut `leaderboard.db`)                   |
   | `CHESSCOM_CONTACT`  | contact mis dans le User-Agent chess.com                |
   | `VS_MONTHS`         | profondeur de `/vs` en mois (défaut `6`)                |
   | `GAMES_TIME_CLASSES`| filtre par défaut des parties comptées (ex. `rapid`)   |

4. **Persistance** : la base SQLite vit dans `/home/container` (le volume du
   serveur), donc elle survit aux redémarrages — rien à configurer.

> Détection de démarrage : l'egg considère le bot « en ligne » quand il logge
> `Connecté en tant que …`. Arrêt propre via `^C` (SIGINT).

### Railway

1. Push ce dossier sur GitHub, puis **New Project → Deploy from repo**.
2. Variable : `DISCORD_TOKEN`. Start command : `python bot.py`.
3. **Important** : le filesystem Railway est éphémère → la base SQLite serait
   effacée à chaque redéploiement (tous les inscrits perdus). Ajoute un
   **Volume** (ex. monté sur `/data`) et mets la variable :
   ```
   DB_PATH=/data/leaderboard.db
   ```

## Notes API chess.com

- Read-only, sans clé. Requêtes faites **en série** (sinon erreur 429).
- Les archives mensuelles sont mises en cache (ETag/`If-None-Match`, stocké
  dans la base SQLite) : la plupart des refreshs ne retéléchargent rien.
- Le rating "rapide" regroupe tous les contrôles rapides (10|0, 15|10…). Pour du
  10|0 strict il faudrait parser les parties — non implémenté ici.
- Les types de parties comptées (boards 1, 3 et 4, annonces) se règlent avec
  `/config timeclass rapid,blitz` (ou `all`), à défaut la variable d'env
  `GAMES_TIME_CLASSES`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
