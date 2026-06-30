"""Client minimal pour la Published-Data API de chess.com.

Règles importantes de l'API :
  - read-only, pas de clé/auth
  - requêtes EN SERIE (jamais en parallèle) sinon 429 Too Many Requests
  - User-Agent recommandé avec un contact
"""

import asyncio
import aiohttp

BASE = "https://api.chess.com/pub"

# ⚠️ Mets ton vrai contact ici (email / pseudo discord). En cas de souci,
# chess.com pourra te prévenir au lieu de te bloquer direct.
USER_AGENT = "discord-chess-leaderboard/1.0 (contact: TON_EMAIL_OU_PSEUDO)"


class ChessComClient:
    def __init__(self, delay: float = 0.3):
        # petite pause entre 2 requêtes pour rester gentil avec l'API
        self.delay = delay
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, url: str, _retries: int = 2):
        assert self._session is not None
        async with self._session.get(url) as r:
            if r.status == 404:
                return None
            if r.status == 429 and _retries > 0:
                await asyncio.sleep(5)
                return await self._get(url, _retries - 1)
            r.raise_for_status()
            data = await r.json()
        await asyncio.sleep(self.delay)
        return data

    async def player_exists(self, username: str) -> bool:
        return (await self._get(f"{BASE}/player/{username.lower()}")) is not None

    async def get_stats(self, username: str):
        return await self._get(f"{BASE}/player/{username.lower()}/stats")

    async def get_archive(self, username: str, year: int, month: int):
        return await self._get(
            f"{BASE}/player/{username.lower()}/games/{year}/{month:02d}"
        )
