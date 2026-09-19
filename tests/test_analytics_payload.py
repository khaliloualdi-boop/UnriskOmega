"""Tests for the briefing assembler.

Loads the real store so the contract is exercised end to end, and asserts it
generates and serialises for *every* portfolio with no CASE-xxx dependency.
"""
import json
from pathlib import Path

import pytest

from analytics.payload import SCHEMA_VERSION, build_briefing
from loader import load_store_from_files
from processing.dossier import select_portfolio

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store():
    return load_store_from_files(ROOT / "clients.json", ROOT / "reference.json")


@pytest.fixture(scope="module")
def cohort(store):
    from analytics.peers import build_cohort
    return build_cohort(store)


def test_peer_cohort_excludes_consolidated_views(store, cohort):
    assert len(cohort) == 56
    assert all(
        (store.portfolios[row["portfolio_id"]].investment_service_name or "").strip()
        != "Consolidated"
        for row in cohort
    )


def test_contract_top_level_shape(store, cohort):
    p = build_briefing(store, "CASE-002", n_paths=200, cohort=cohort)
    for key in ("schema_version", "generated_at", "analysis_date", "temporal", "profile",
                "performance", "allocation", "top_holdings", "accounts_by_currency",
                "mandate", "peers", "projection", "market_context", "data_quality", "disclaimer"):
        assert key in p, f"missing {key}"
    assert p["schema_version"] == SCHEMA_VERSION
    assert set(p["projection"]) == {"bootstrap", "parametric"}
    # analysis_date is the history end, not the run date
    assert p["analysis_date"] and p["analysis_date"] != p["generated_at"]


def test_unknown_profile_is_unavailable(store, cohort):
    p = build_briefing(store, "CASE-999", n_paths=50, cohort=cohort)
    assert p["status"] == "unavailable"


def test_accepts_portfolio_id(store, cohort):
    ref = "CASE-002"
    pid = select_portfolio(store, ref)
    p = build_briefing(store, pid, n_paths=50, cohort=cohort)
    assert p["profile"]["portfolio_id"] == pid


def test_data_quality_present_and_lists_unavailable(store, cohort):
    # a profile with no security positions still assembles, and says what's missing
    p = build_briefing(store, "CASE-002", n_paths=50, cohort=cohort)
    dq = p["data_quality"]
    assert "stage_status" in dq and "notes" in dq and "conventions" in dq
    assert isinstance(dq["unavailable"], list)


def test_news_context_is_attached_without_recalculation(store, cohort):
    news = {
        "schema_version": "news-2.0",
        "status": "ok",
        "articles": [{"title": "Portfolio-specific update"}],
    }
    payload = build_briefing(
        store,
        "CASE-002",
        n_paths=50,
        cohort=cohort,
        news_result=news,
    )
    assert payload["market_context"] is news


def test_missing_news_keeps_explicit_placeholder(store, cohort):
    payload = build_briefing(store, "CASE-002", n_paths=50, cohort=cohort)
    assert payload["market_context"]["status"] == "to_be_provided_by_market_data_team"


def test_generates_and_serialises_for_every_portfolio(store, cohort):
    """No business rule may depend on a particular CASE-xxx id."""
    failures = []
    for pid in store.portfolios:
        try:
            payload = build_briefing(store, pid, n_paths=50, cohort=cohort)
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except Exception as e:  # noqa: BLE001
            failures.append((pid, repr(e)))
    assert not failures, failures
