# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt   # inclut requirements.txt + pytest
cp .env.example .env                  # then set DISCORD_TOKEN
python bot.py                         # run the bot
pytest                                # run tests (pytest -k <name> for one test)
```

There is no linter or build step. `DISCORD_TOKEN` is required to run; set `GUILD_ID` in dev for instant slash-command sync (global sync can take up to 1h to propagate). Tests need no token: they only import `db`, `leaderboards`, `chesscom` (never `bot`, which reads `DISCORD_TOKEN` at import time).

## What it is

A Discord bot for a single server that maintains 4 chess.com leaderboards (games played this week, rapid Elo with weekly delta, wins vs other registered members, weekly Elo progression) in one channel, auto-refreshed at 00h/06h/12h/18h (`TZ_NAME`, default Europe/Paris). It also posts a frozen weekly recap on Mondays (winner of the past week) and announces each new game played between registered members. Registered users only (`/join`). Docs, comments, and user-facing strings are in French.

## Architecture

Four modules, one direction of dependency: `bot.py` → `leaderboards.py` → `chesscom.py`, with `db.py` used by `bot.py` and tests only (`chesscom.py` must NOT import `db` — the HTTP cache is injected as an adapter, `db.SqliteHttpCache`).

- **`bot.py`** — discord.py commands, the scheduled loop, embed rendering, announcements. Core is `do_refresh()`: one `gather()` call, then weekly recap (if the week changed), rating snapshots, **edits** of the 4 persistent messages (IDs in the `config` table; a missing/deleted message is re-sent and its ID stored — this is also how new boards appear on existing installs without re-running `/setup`), then game announcements. `scheduled_refresh` wraps `do_refresh()` in try/except: an unhandled exception would permanently kill the `tasks.loop`. `API_LOCK` (asyncio.Lock) serializes every `ChessComClient` use across commands and the scheduler — chess.com bans parallel requests (429).
- **`leaderboards.py`** — pure computation, all testable with fakes (`now` is injectable). `gather()` returns `(data, member_games)`: per-player weekly counters (plus `prev_*` counters for the recap when `prev_week=True`) computed in a single pass over 1-3 monthly archives (weeks straddling months), and the deduplicated list of member-vs-member games for announcements. A player whose fetch fails is skipped (logged), not fatal. `head_to_head()` (for `/vs`) reads only the invoker's archives over `VS_MONTHS` months. `parse_time_classes()` validates the `/config timeclass` filter.
- **`chesscom.py`** — async client for the chess.com PubAPI. Hard constraint: requests must be **serial, never parallel**; the client sleeps `delay` between calls and retries on 429. 404 returns `None`. Monthly archives are fetched with `If-None-Match`/`If-Modified-Since` when a cache adapter is injected; on 304 the cached body is served (a 304 has no body — never call `r.json()` on it). The User-Agent should carry a contact (`CHESSCOM_CONTACT` env var).
- **`db.py`** — SQLite (`DB_PATH`, default `leaderboard.db`). Tables: `players`, `config` (key/value: channel + 4 message IDs, `time_classes`, `last_recap_week`, `announce_bootstrap`), `rating_history` (snapshots per refresh → weekly Elo deltas, purged after 90 days), `announced_games` (announcement dedup), `http_cache` (gzip-compressed archive JSON + ETag, purged after 60 days unused). All timestamps are UTC ISO strings — SQLite comparisons are string comparisons, so never mix timezones; convert the local `_week_start()` to UTC before querying. Migration is `CREATE TABLE IF NOT EXISTS` only; `init()` must always pass on an existing production DB.

First-run guards (never post on a fresh state): `announce_bootstrap` config key absent → mark all current games announced silently; `last_recap_week` absent → set it without posting; no rating snapshots → no deltas.

## Tests

`tests/conftest.py` provides `tmp_db` (module `db` monkeypatched onto a throwaway SQLite file) and `FakeClient` (pre-wired stats/archives, per-user failure injection). `test_chesscom.py` uses a hand-rolled fake aiohttp session. pytest-asyncio runs in `auto` mode (bare `async def` tests).

## Deployment

- **`egg-chess-leaderboard.json`** is a Pelican panel egg: it clones this repo at install, installs `requirements.txt` (not `-dev`) and runs `bot.py` on each start. Its "online" detection matches the log line `Connecté en tant que` — don't change that `on_ready` print without updating the egg. Keep the egg's `variables` in sync when adding env vars; never add a *required* one (existing servers must restart unreconfigured).
- On ephemeral filesystems (Railway), `DB_PATH` must point to a persistent volume or all registrations are lost on redeploy.
