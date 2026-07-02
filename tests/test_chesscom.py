import pytest

import chesscom
from chesscom import ChessComClient


class FakeResponse:
    def __init__(self, status, json_data=None, headers=None):
        self.status = status
        self._json = json_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        assert self._json is not None, "json() appelé sur une réponse sans corps (304 ?)"
        return self._json

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []  # (url, headers)

    def get(self, url, headers=None):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)


class DictCache:
    def __init__(self):
        self.store = {}

    def get(self, url):
        return self.store.get(url)

    def set(self, url, etag, last_modified, body):
        self.store[url] = {"etag": etag, "last_modified": last_modified, "body": body}


def make_client(responses, cache=None):
    client = ChessComClient(delay=0, cache=cache)
    client._session = FakeSession(responses)
    return client


async def test_archive_cached_then_304_served_from_cache():
    cache = DictCache()
    body = {"games": [{"uuid": "g1"}]}
    client = make_client(
        [FakeResponse(200, body, {"ETag": '"abc"'}), FakeResponse(304)],
        cache=cache,
    )
    assert await client.get_archive("alice", 2026, 6) == body
    # 2e appel : header conditionnel envoyé, corps servi depuis le cache
    assert await client.get_archive("alice", 2026, 6) == body
    assert client._session.requests[1][1].get("If-None-Match") == '"abc"'


async def test_last_modified_also_sent():
    cache = DictCache()
    client = make_client(
        [FakeResponse(200, {}, {"Last-Modified": "lundi"}), FakeResponse(304)],
        cache=cache,
    )
    await client.get_archive("alice", 2026, 6)
    await client.get_archive("alice", 2026, 6)
    assert client._session.requests[1][1].get("If-Modified-Since") == "lundi"


async def test_stats_not_cached():
    cache = DictCache()
    client = make_client([FakeResponse(200, {"x": 1}, {"ETag": '"abc"'})], cache=cache)
    assert await client.get_stats("alice") == {"x": 1}
    assert cache.store == {}


async def test_no_cache_object_is_fine():
    client = make_client([FakeResponse(200, {"games": []})])
    assert await client.get_archive("alice", 2026, 6) == {"games": []}


async def test_404_returns_none():
    client = make_client([FakeResponse(404)])
    assert await client.get_stats("ghost") is None


async def test_429_retries(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(chesscom.asyncio, "sleep", fake_sleep)
    client = make_client([FakeResponse(429), FakeResponse(200, {"ok": True})])
    assert await client.get_stats("alice") == {"ok": True}
    assert 5 in sleeps


async def test_429_gives_up_after_retries(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(chesscom.asyncio, "sleep", fake_sleep)
    client = make_client([FakeResponse(429)] * 3)
    with pytest.raises(RuntimeError):
        await client.get_stats("alice")
