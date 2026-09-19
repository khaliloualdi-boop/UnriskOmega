"""Cache public articles and bound paid runs across parallel searches.

Only public queries/articles are persisted. SQLite reservations prevent duplicate
runs across processes; a lock protects transactions and budgets within a process.
"""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .models import NewsBatch, NewsProviderError


class CachedNewsProvider:
    def __init__(self, provider, path, *, max_runs=4, ttl_seconds=3600, clock=None):
        if not isinstance(max_runs, int) or isinstance(max_runs, bool) or not 0 <= max_runs <= 200:
            raise ValueError("max_runs must be an integer between 0 and 200.")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 86400:
            raise ValueError("Cache TTL must be between 60 and 86400 seconds.")
        self.provider = provider
        self.name, self.is_fixture = provider.name, provider.is_fixture
        self.max_runs, self.ttl_seconds = max_runs, ttl_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stats = {"provider_calls": 0, "cache_hits": 0, "budget_blocked": 0}
        self._memory = {}
        self._lock = threading.RLock()
        self._closed = False
        self._db = None
        if path is not None:
            path = Path(path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(path, timeout=1, check_same_thread=False)
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS article_cache "
                    "(key TEXT PRIMARY KEY, expires REAL NOT NULL, state TEXT NOT NULL, payload TEXT)"
                )
                self._db.execute("DELETE FROM article_cache WHERE expires < ?", (self.clock().timestamp(),))
                self._db.commit()
            except (OSError, sqlite3.Error):
                if self._db is not None:
                    self._db.close()
                raise NewsProviderError("News cache unavailable; no paid run was started.") from None

    def close(self):
        # In-flight HTTP requests may finish after the collector's deadline.
        # They can return their data but must not use this closed connection.
        with self._lock:
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None

    def _get(self, key, now):
        if self._db is None:
            entry = self._memory.get(key)
        else:
            entry = self._db.execute(
                "SELECT expires, state, payload FROM article_cache WHERE key=?", (key,),
            ).fetchone()
        return entry if entry and entry[0] > now else None

    def _set(self, key, expires, state, payload):
        if self._db is None:
            self._memory[key] = (expires, state, payload)
        else:
            self._db.execute("INSERT OR REPLACE INTO article_cache VALUES (?, ?, ?, ?)",
                             (key, expires, state, payload))

    def search(self, query, *, limit, lookback_days, deadline=None):
        key = hashlib.sha256(json.dumps([
            "public-articles-v1", self.name, self.is_fixture, "en", "lang_en", "date",
            " ".join(query.split()), limit, lookback_days,
        ]).encode()).hexdigest()
        with self._lock:
            if self._closed:
                raise NewsProviderError("News cache is closed.")
            now = self.clock().timestamp()
            try:
                if self._db is not None:
                    self._db.execute("BEGIN IMMEDIATE")
                entry = self._get(key, now)
                if entry and entry[1] != "ready":
                    raise NewsProviderError("Identical search is pending or in cooldown after a failed run.")
                if entry:
                    try:
                        data = json.loads(entry[2])
                        stamp = datetime.fromisoformat(data["retrieved_at"])
                        if stamp.tzinfo is None or stamp.timestamp() > now + 300 or not isinstance(data["items"], list):
                            raise ValueError("invalid cached batch")
                        batch = NewsBatch(data["items"], stamp, data.get("run_id"))
                    except (ValueError, TypeError, KeyError):
                        batch = None
                    if batch is not None:
                        self.stats["cache_hits"] += 1
                        return batch
                if deadline is not None and time.monotonic() >= deadline:
                    raise NewsProviderError("News search time limit reached; no paid run was started.")
                if self.stats["provider_calls"] >= self.max_runs:
                    self.stats["budget_blocked"] += 1
                    raise NewsProviderError("Run budget exhausted; no new Apify run was started.")
                self._set(key, now + 600, "pending", None)
            except sqlite3.Error:
                raise NewsProviderError("News cache unavailable; no paid run was started.") from None
            finally:
                if self._db is not None:
                    try:
                        self._db.commit()
                    except sqlite3.Error:
                        self._db.rollback()
                        raise NewsProviderError("Cache reservation could not be saved; no paid run was started.") from None
            # Reserve the budget under the same lock, before releasing for HTTP.
            self.stats["provider_calls"] += 1

        from .apify import ApifyNewsProvider
        options = {"deadline": deadline} if isinstance(
            self.provider, (ApifyNewsProvider, CachedNewsProvider)
        ) else {}
        try:
            batch = self.provider.search(query, limit=limit, lookback_days=lookback_days, **options)
            if not isinstance(batch, NewsBatch) or not isinstance(batch.items, list):
                raise NewsProviderError("Provider returned an invalid batch.")
            stamp = batch.retrieved_at
            if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                raise NewsProviderError("Provider returned an invalid retrieval date.")
            payload = json.dumps({"items": batch.items, "retrieved_at": stamp.isoformat(),
                                  "run_id": batch.run_id}, allow_nan=False)
        except (NewsProviderError, ValueError, TypeError):
            raise NewsProviderError("Provider failed; identical query is in cooldown, no automatic retry.") from None
        with self._lock:
            if not self._closed:
                try:
                    self._set(key, min(self.clock().timestamp(), stamp.timestamp()) + self.ttl_seconds,
                              "ready", payload)
                    if self._db is not None:
                        self._db.commit()
                except sqlite3.Error:
                    # Keep fetched data even when persistence fails; never rerun.
                    if self._db is not None:
                        self._db.rollback()
        return batch
