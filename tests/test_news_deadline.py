import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from news.models import NewsBatch, NewsProviderError
from news.service import _search_batches


def test_deadline_preserves_completed_batch_without_waiting_for_stalled_provider():
    release = threading.Event()
    messages = []

    class Provider:
        def search(self, query, **kwargs):
            if query == "slow":
                release.wait(2)
            return NewsBatch([], datetime.now(UTC))

    start = time.monotonic()
    try:
        batches = _search_batches(
            [SimpleNamespace(text="fast"), SimpleNamespace(text="slow")],
            Provider(), 10, 7, .1, 2, messages.append,
        )
        assert time.monotonic() - start < 1
        assert isinstance(batches[0], NewsBatch)
        assert isinstance(batches[1], NewsProviderError)
        assert any("Continuing" in message for message in messages)
    finally:
        release.set()


def test_query_failures_preserve_other_results():
    class Provider:
        def search(self, query, **kwargs):
            if query == "failed":
                raise NewsProviderError("Service unavailable")
            return NewsBatch([], datetime.now(UTC))

    batches = _search_batches(
        [SimpleNamespace(text="failed"), SimpleNamespace(text="ok")],
        Provider(), 10, 7, 1, 2, lambda message: None,
    )
    assert isinstance(batches[0], NewsProviderError)
    assert isinstance(batches[1], NewsBatch)


def test_poll_timeout_attempts_abort_without_restarting_paid_run():
    import pytest

    from news.apify import ApifyNewsProvider

    class Transport:
        def __init__(self):
            self.paths = []

        def request(self, method, path, body=None):
            self.paths.append(path)
            if path.startswith("actors/"):
                return {"data": {"id": "run123", "status": "RUNNING"}}
            if path.endswith("/abort"):
                return {}
            raise NewsProviderError("Timed out")

    transport = Transport()
    with pytest.raises(NewsProviderError, match="Timed out"):
        ApifyNewsProvider(transport=transport).search("Nestle")
    assert transport.paths[-1] == "actor-runs/run123/abort"
    assert sum(path.startswith("actors/") for path in transport.paths) == 1
