"""Cache store tests."""

import time
from pathlib import Path

from appcatalog_mcp.cache import CacheStore


def test_cache_set_get(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache.sqlite", ttl_seconds=60)
    cache.set("key", {"value": 1})
    assert cache.get("key") == {"value": 1}


def test_cache_ttl_expiry(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache.sqlite", ttl_seconds=1)
    cache.set("key", {"value": 1})
    assert cache.get("key") == {"value": 1}
    time.sleep(1.1)
    assert cache.get("key") is None


def test_cache_overwrite(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache.sqlite", ttl_seconds=60)
    cache.set("key", {"v": 1})
    cache.set("key", {"v": 2})
    assert cache.get("key") == {"v": 2}


def test_cache_delete(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache.sqlite", ttl_seconds=60)
    cache.set("key", "value")
    cache.delete("key")
    assert cache.get("key") is None


def test_cache_purge_expired(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache.sqlite", ttl_seconds=1)
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    time.sleep(1.1)
    purged = cache.purge_expired()
    assert purged == 2
    assert cache.get("k1") is None
    assert cache.get("k2") is None


def test_cache_persists_across_instances(tmp_path: Path):
    db = tmp_path / "cache.sqlite"
    CacheStore(db, ttl_seconds=60).set("k", "v")
    assert CacheStore(db, ttl_seconds=60).get("k") == "v"
