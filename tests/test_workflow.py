import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_engine import BriefingGenerationError
from news.models import NewsBatch, NewsProviderError
from workflow import case_files, load_case, run_workflow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def new_case(tmp_path):
    clients = json.loads((ROOT / "clients.json").read_text())
    client = clients[1]
    client["ClientRef"] = "UNSEEN-CLIENT"
    client["Portfolios"][0]["PortfolioId"] = 987654
    case = tmp_path / "case.json"
    case.write_text(json.dumps(client))
    return case


def test_unseen_client_and_replacement(new_case):
    store, old = load_case(new_case, ROOT / "reference.json")
    assert "UNSEEN-CLIENT" in store.clients
    raw = json.loads(new_case.read_text())
    raw["FirstName"] = "Changed"
    new_case.write_text(json.dumps(raw))
    _, new = load_case(new_case, ROOT / "reference.json")
    assert new != old


@pytest.mark.parametrize("wrapper", [lambda c: [c], lambda c: {"Clients": [c]}])
def test_case_containers(new_case, wrapper):
    new_case.write_text(json.dumps(wrapper(json.loads(new_case.read_text()))))
    store, _ = load_case(new_case, ROOT / "reference.json")
    assert "UNSEEN-CLIENT" in store.clients


def test_discovery_prefers_inbox(tmp_path):
    assert case_files(tmp_path) == [tmp_path / "clients.json"]
    (tmp_path / "inputs").mkdir()
    case = tmp_path / "inputs" / "new.json"
    case.write_text("[]")
    assert case_files(tmp_path) == [case]


class EmptyProvider:
    name = "offline-test"
    is_fixture = True

    def search(self, query, **kwargs):
        return NewsBatch([], datetime.now(UTC), "test")


def test_workflow_collects_news_for_unseen_case_and_passes_combined_payload(new_case):
    store, _ = load_case(new_case, ROOT / "reference.json")
    seen = []

    def narrate(payload):
        seen.append(payload)
        return "Generated text"

    result = run_workflow(store, "UNSEEN-CLIENT", 987654,
                          provider_factory=EmptyProvider,
                          narrators={"briefing": narrate, "development": narrate, "news": narrate})
    assert not result["errors"]
    assert len(seen) == 2  # no paid news narration when there are no articles
    assert seen[0]["profile"]["client_ref"] == "UNSEEN-CLIENT"
    assert seen[0]["market_context"]["portfolio_id"] == 987654
    assert seen[0]["market_context"]["status"] in {"no_results", "unavailable"}


def test_service_failure_keeps_analysis(new_case):
    store, _ = load_case(new_case, ROOT / "reference.json")

    def no_provider():
        raise NewsProviderError("Missing token")

    def no_model(payload):
        raise BriefingGenerationError("Model unavailable")

    result = run_workflow(store, "UNSEEN-CLIENT", 987654,
                          provider_factory=no_provider, narrators={"briefing": no_model})
    assert result["payload"]["profile"]["portfolio_id"] == 987654
    assert result["errors"] == {"news": "Missing token", "briefing": "Model unavailable"}


def test_news_cache_reuses_success_but_expires_and_tracks_case(new_case):
    store, _ = load_case(new_case, ROOT / "reference.json")
    cache = {}
    calls = []

    def provider():
        calls.append(1)
        return EmptyProvider()

    def run(key):
        return run_workflow(store, "UNSEEN-CLIENT", 987654,
                            provider_factory=provider, narrators={},
                            news_cache=cache, cache_key=key)

    run("original")
    run("original")
    assert len(calls) == 1
    timestamp, context = cache["original"]
    cache["original"] = (timestamp - 1801, context)
    run("original")
    run("replacement")
    assert len(calls) == 3


def test_app_one_click_and_rerun_do_not_repeat_services(monkeypatch):
    from streamlit.testing.v1 import AppTest

    import workflow

    calls = []
    original = workflow.run_workflow

    def offline(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs, provider_factory=EmptyProvider,
                        narrators={"briefing": lambda payload: "Offline test briefing"})

    monkeypatch.setattr(workflow, "run_workflow", offline)
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    assert not calls
    app.button[0].click().run(timeout=30)
    assert not app.exception
    assert calls == [1]
    app.run(timeout=30)
    assert not app.exception
    assert calls == [1]
    app.selectbox[1].select_index(1).run(timeout=30)
    assert not app.exception
    assert not any(m.value == "Offline test briefing" for m in app.markdown)
