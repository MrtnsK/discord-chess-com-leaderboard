"""Client minimal pour la Published-Data API de chess.com.

Règles importantes de l'API :
  - read-only, pas de clé/auth
  - requêtes EN SERIE (jamais en parallèle) sinon 429 Too Many Requests
  - User-Agent recommandé avec un contact
  - ETag/Last-Modified supportés -> cache conditionnel (304) sur les archives
"""

import os
import asyncio
import aiohttp

BASE = "https://api.chess.com/pub"

# Contact recommandé par l'API chess.com (email / pseudo discord). En cas de
# souci, chess.com peut te prévenir au lieu de te bloquer direct.
# Configurable via la variable d'environnement CHESSCOM_CONTACT (ou
# CHESSCOM_USER_AGENT pour surcharger tout le User-Agent).
_CONTACT = os.environ.get("CHESSCOM_CONTACT", "TON_EMAIL_OU_PSEUDO")
USER_AGENT = os.environ.get(
    "CHESSCOM_USER_AGENT",
    f"discord-chess-leaderboard/1.0 (contact: {_CONTACT})",
)


class ChessComClient:
    def __init__(self, delay: float = 0.3, cache=None):
        # petite pause entre 2 requêtes pour rester gentil avec l'API
        self.delay = delay
        # cache optionnel (méthodes get(url) et set(url, etag, last_modified,
        # body)), injecté par l'appelant pour ne pas coupler ce module à la DB
        self.cache = cache
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, url: str, cacheable: bool = False, _retries: int = 2):
        assert self._session is not None
        cached = self.cache.get(url) if (cacheable and self.cache) else None
        headers = {}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached and cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

        async with self._session.get(url, headers=headers) as r:
            if r.status == 404:
                return None
            if r.status == 429 and _retries > 0:
                await asyncio.sleep(5)
                return await self._get(url, cacheable, _retries - 1)
            if r.status == 304 and cached:
                # inchangé depuis la dernière fois : corps servi depuis le
                # cache (un 304 n'a pas de corps, ne jamais appeler r.json())
                data = cached["body"]
            else:
                r.raise_for_status()
                data = await r.json()
                if cacheable and self.cache and (
                    r.headers.get("ETag") or r.headers.get("Last-Modified")
                ):
                    self.cache.set(
                        url, r.headers.get("ETag"), r.headers.get("Last-Modified"), data
                    )
        await asyncio.sleep(self.delay)
        return data

    async def player_exists(self, username: str) -> bool:
        return (await self._get(f"{BASE}/player/{username.lower()}")) is not None

    async def get_stats(self, username: str):
        return await self._get(f"{BASE}/player/{username.lower()}/stats")

    async def get_archive(self, username: str, year: int, month: int):
        return await self._get(
            f"{BASE}/player/{username.lower()}/games/{year}/{month:02d}",
            cacheable=True,
        )
