"""Regressions for the v3 cache inside the current parallel/deadline workflow."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from news.apify import ApifyNewsProvider
from news.cache import CachedNewsProvider
from news.models import NewsBatch, NewsProviderError
from news.service import _search_batches


class Provider:
    name = "parallel-offline"
    is_fixture = True

    def search(self, query, **kwargs):
        return NewsBatch([], datetime.now(UTC), query)


def test_sqlite_cache_works_in_workers_and_reuses_runs(tmp_path):
    cache = CachedNewsProvider(Provider(), tmp_path / "news.db")
    queries = [SimpleNamespace(text=value) for value in ("one", "two", "three", "four")]
    try:
        for _ in range(2):
            results = _search_batches(queries, cache, 6, 7, 2, 4, lambda _: None)
            assert all(isinstance(result, NewsBatch) for result in results)
        assert cache.stats == {"provider_calls": 4, "cache_hits": 4, "budget_blocked": 0}
    finally:
        cache.close()


def test_parallel_workers_cannot_exceed_budget(tmp_path):
    cache = CachedNewsProvider(Provider(), tmp_path / "news.db", max_runs=1)
    queries = [SimpleNamespace(text=str(i)) for i in range(8)]
    try:
        results = _search_batches(queries, cache, 6, 7, 2, 8, lambda _: None)
        assert sum(isinstance(result, NewsBatch) for result in results) == 1
        assert cache.stats["provider_calls"] == 1
        assert cache.stats["budget_blocked"] == 7
    finally:
        cache.close()


def test_duplicate_worker_does_not_launch_second_run(tmp_path):
    started, release = threading.Event(), threading.Event()

    class BlockingProvider(Provider):
        def search(self, query, **kwargs):
            started.set()
            assert release.wait(2)
            return super().search(query, **kwargs)

    cache = CachedNewsProvider(BlockingProvider(), tmp_path / "news.db")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.search, "same", limit=6, lookback_days=7)
        try:
            assert started.wait(1)
            with pytest.raises(NewsProviderError, match="pending"):
                cache.search("same", limit=6, lookback_days=7)
            assert cache.stats["provider_calls"] == 1
        finally:
            release.set()
        assert isinstance(future.result(timeout=1), NewsBatch)
    cache.close()


def test_cache_can_close_before_an_inflight_search_finishes(tmp_path):
    started, release = threading.Event(), threading.Event()

    class BlockingProvider(Provider):
        def search(self, query, **kwargs):
            started.set()
            assert release.wait(2)
            return super().search(query, **kwargs)

    cache = CachedNewsProvider(BlockingProvider(), tmp_path / "news.db")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.search, "slow", limit=6, lookback_days=7)
        try:
            assert started.wait(1)
            cache.close()
        finally:
            release.set()
        assert isinstance(future.result(timeout=1), NewsBatch)
    with pytest.raises(NewsProviderError, match="closed"):
        cache.search("later", limit=6, lookback_days=7)


def test_cache_forwards_deadline_to_apify_and_blocks_expired_work(tmp_path):
    provider = ApifyNewsProvider(transport=object())
    cache = CachedNewsProvider(provider, tmp_path / "news.db")
    deadline = time.monotonic() + 10
    try:
        with patch.object(provider, "search", return_value=NewsBatch([], datetime.now(UTC))) as search:
            cache.search("one", limit=6, lookback_days=7, deadline=deadline)
            assert search.call_args.kwargs["deadline"] == deadline
            with pytest.raises(NewsProviderError, match="time limit"):
                cache.search("two", limit=6, lookback_days=7, deadline=time.monotonic() - 1)
            assert search.call_count == 1
    finally:
        cache.close()
