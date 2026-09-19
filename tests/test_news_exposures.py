"""Offline contract/regression tests; no tests start a paid API run."""
import copy
import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_news import NOW, ROOT, FakeProvider, article, dossier

from loader import load_store_from_files
from news.audit import audit_store
from news.cache import CachedNewsProvider
from news.context import build_news_context, context_from_briefing
from news.models import NewsContext, NewsProviderError
from news.planning import plan_news
from news.service import collect_news, render_news_context
from news_integration import news_block
from processing.contracts import to_jsonable
from processing.dossier import build_dossier


@pytest.fixture(scope="module")
def store():
    return load_store_from_files(ROOT / "clients.json", ROOT / "reference.json")


def cash_context(d, code="CHF", weight=None):
    return NewsContext(d.client_ref, d.portfolio_id, "CHF", {
        "accounts_by_currency": {
            "status": "calculated", "weight_basis": "unknown" if weight is None else "all positions",
            "by_currency": [{"category": "cash", "currency": code, "value": 500,
                             "weight_of_total": weight}],
        },
    })


def test_all_portfolios_have_traceable_plans_without_paid_calls(store):
    report = audit_store(store, as_of=NOW)
    assert report["summary"]["portfolios"] == 57
    assert report["summary"]["clients"] == 47
    assert report["summary"]["without_queries"] == 0
    assert report["summary"]["unique_queries"] < report["summary"]["planned_queries"]
    for row in report["portfolios"]:
        for query in row["plan"]["queries"]:
            assert query["evidence"]
            assert query["holdings"] or query["exposures"]
            assert len(query["text"]) <= 240
        assert "result" not in row
    json.dumps(report, allow_nan=False)


def test_context_does_not_run_simulations_or_mutate_dossier(store):
    d = build_dossier(store, "CASE-002", analysis_date=NOW.date())
    before = to_jsonable(d)
    with patch("analytics.simulation.analyze_projection", side_effect=AssertionError("unexpected simulation")):
        context = build_news_context(store, d)
        plan_news(d, context=context)
    assert context.blocks["top_holdings"]["status"] == "calculated"
    assert before == to_jsonable(d)


@pytest.mark.parametrize("profile", [{}, {"client_ref": "wrong", "portfolio_id": 1},
                                     {"client_ref": "PRIVATE-CLIENT", "portfolio_id": 2}])
def test_wrong_briefing_identity_rejected(profile):
    with pytest.raises(ValueError):
        context_from_briefing({"profile": profile}, dossier())


def test_context_copies_supplied_blocks():
    d = dossier()
    payload = {"profile": {"client_ref": d.client_ref, "portfolio_id": d.portfolio_id,
                           "portfolio_currency": d.portfolio.portfolio_currency},
               "allocation": {"status": "unavailable"}}
    context = context_from_briefing(payload, d)
    context.blocks["allocation"]["status"] = "changed"
    assert payload["allocation"]["status"] == "unavailable"


def test_unseen_client_cash_has_specific_policy_query_not_generic_fill():
    d = dossier([])
    d.client_ref = "NEW-UNSEEN-CLIENT"
    plan = plan_news(d, context=cash_context(d))
    assert "Swiss National Bank" in plan.queries[0].text
    assert plan.queries[0].exposures[0].weight is None
    assert "NEW-UNSEEN" not in plan.queries[0].text


def test_foreign_cash_has_fx_pair_and_never_a_fabricated_weight():
    d = dossier([])
    plan = plan_news(d, context=cash_context(d, "EUR"))
    assert any("EUR/CHF" in q.text for q in plan.queries)
    assert all(e.weight is None for q in plan.queries for e in q.exposures)


@pytest.mark.parametrize("code", ["UNKNOWN", "NOTACURRENCY"])
def test_unknown_cash_is_not_mislabeled(code):
    d = dossier([])
    plan = plan_news(d, context=cash_context(d, code))
    assert not plan.queries
    assert any(x["reason"] == "unmapped_cash_currency" for x in plan.skipped)


def test_macro_requires_subject_and_event_and_preserves_evidence():
    d = dossier([])
    rows = [
        article(title="Swiss National Bank opens museum", snippet="Art exhibition.", url="https://news.example/art"),
        article(title="Football inflation debate", snippet="Sports news.", url="https://news.example/sport"),
        article(title="Swiss National Bank cuts interest rates", snippet="Policy decision.", url="https://news.example/snb"),
    ]
    result = collect_news(d, FakeProvider(rows), context=cash_context(d), as_of=NOW)
    assert [a.url for a in result.articles] == ["https://news.example/snb"]
    match = result.articles[0].matches[0]
    assert match.exposures[0].category == "CHF"
    assert "interest rates" in match.supporting_terms
    assert result.articles[0].impact_status == "not_assessed"
    assert result.schema_version == "news-3.0"
    assert json.loads(render_news_context(result))["articles"][0]["matches"][0]["evidence"]


def test_same_candidates_are_filtered_again_for_each_client():
    d = dossier([])
    rows = [article(title="Swiss National Bank changes monetary policy", snippet="", url="https://news.example/ch"),
            article(title="European Central Bank changes monetary policy", snippet="", url="https://news.example/eu")]
    ch = collect_news(d, FakeProvider(rows), context=cash_context(d), as_of=NOW)
    eu = collect_news(d, FakeProvider(rows), context=cash_context(d, "EUR"), as_of=NOW)
    assert [a.url for a in ch.articles] == ["https://news.example/ch"]
    assert [a.url for a in eu.articles] == ["https://news.example/eu"]


def test_bad_region_labels_are_never_search_terms():
    d = dossier([])
    ctx = NewsContext(d.client_ref, d.portfolio_id, "CHF", {
        "lookthrough": {"status": "calculated", "dimensions": {
            "region": [{"category": "Financials", "weight": 1.0}],
            "industry": [{"category": "Unclassified", "weight": 1.0}],
        }},
    })
    assert not plan_news(d, context=ctx).queries


def test_fund_exposure_uses_industry_without_inventing_companies():
    d = dossier([])
    ctx = NewsContext(d.client_ref, d.portfolio_id, "CHF", {
        "lookthrough": {"status": "calculated", "weight_basis": "securities book",
                        "dimensions": {"industry": [{"category": "Energy", "weight": 0.8}]}},
    })
    plan = plan_news(d, context=ctx)
    assert len(plan.queries) == 1
    assert "energy sector" in plan.queries[0].text
    assert not plan.queries[0].holdings
    assert plan.queries[0].exposures[0].basis == "securities book"


def test_no_double_counting_accounts_and_currency_allocation():
    d = dossier([])
    ctx = cash_context(d, weight=0.3)
    ctx.blocks["allocation"] = {"status": "calculated", "allocation": {
        "currency_group": {"weights": {"Swiss francs": 0.8}, "basis": "all positions"},
    }}
    plan = plan_news(d, context=ctx)
    assert len(plan.queries) == 1
    assert plan.queries[0].exposures[0].weight == 0.8
    assert plan.queries[0].priority < 0.8


def test_cache_hit_across_provider_instances_has_no_new_call(tmp_path):
    path = tmp_path / "articles.sqlite3"
    first = FakeProvider()
    cache = CachedNewsProvider(first, path, clock=lambda: NOW)
    original = cache.search("Nestle", limit=10, lookback_days=7)
    cache.close()
    second = FakeProvider()
    cache = CachedNewsProvider(second, path, max_runs=0, clock=lambda: NOW)
    try:
        hit = cache.search("Nestle", limit=10, lookback_days=7)
        assert hit.items == original.items
        assert hit.retrieved_at == original.retrieved_at
        assert not second.calls
        assert cache.stats["cache_hits"] == 1
    finally:
        cache.close()


def test_cache_ttl_and_parameter_keys(tmp_path):
    provider = FakeProvider()
    cache = CachedNewsProvider(provider, tmp_path / "cache.db", clock=lambda: NOW, max_runs=4)
    try:
        cache.search("Nestle", limit=10, lookback_days=7)
        cache.search("Nestle", limit=10, lookback_days=1)
        cache.clock = lambda: NOW + timedelta(hours=2)
        cache.search("Nestle", limit=10, lookback_days=7)
        assert len(provider.calls) == 3
    finally:
        cache.close()


def test_global_budget_preserves_cache_hits(tmp_path):
    provider = FakeProvider()
    cache = CachedNewsProvider(provider, tmp_path / "cache.db", max_runs=1, clock=lambda: NOW)
    try:
        cache.search("Nestle", limit=10, lookback_days=7)
        with pytest.raises(NewsProviderError, match="budget"):
            cache.search("Microsoft", limit=10, lookback_days=7)
        cache.search("Nestle", limit=10, lookback_days=7)
        assert len(provider.calls) == 1
        assert cache.stats == {"provider_calls": 1, "cache_hits": 1, "budget_blocked": 1}
    finally:
        cache.close()


def test_failed_run_is_not_immediately_retried(tmp_path):
    provider = FakeProvider(fail_first=True)
    cache = CachedNewsProvider(provider, tmp_path / "cache.db", clock=lambda: NOW)
    try:
        for _ in range(2):
            with pytest.raises(NewsProviderError):
                cache.search("Nestle", limit=10, lookback_days=7)
        assert len(provider.calls) == 1
    finally:
        cache.close()


def test_second_process_cannot_duplicate_pending_run(tmp_path):
    path = tmp_path / "cache.db"
    second = CachedNewsProvider(FakeProvider(), path, clock=lambda: NOW)

    class DuringRequest(FakeProvider):
        def search(self, query, **kwargs):
            with pytest.raises(NewsProviderError, match="pending"):
                second.search(query, **kwargs)
            return super().search(query, **kwargs)

    first = CachedNewsProvider(DuringRequest(), path, clock=lambda: NOW)
    try:
        first.search("Nestle", limit=10, lookback_days=7)
        assert second.stats["provider_calls"] == 0
    finally:
        first.close()
        second.close()


def test_cached_old_articles_do_not_become_fresh(tmp_path):
    d = dossier()
    cache = CachedNewsProvider(FakeProvider([article(publishedAt="8 days ago")]),
                               tmp_path / "cache.db", clock=lambda: NOW)
    try:
        result = collect_news(d, cache, as_of=NOW)
        assert not result.articles
        assert result.rejected_counts["outside_time_window"] >= 1
    finally:
        cache.close()


@pytest.mark.parametrize("change", [
    {"client_ref": "wrong"}, {"portfolio_id": 999}, {"mode": "query_plan"},
    {"schema_version": "unknown"}, {"articles": "bad"},
])
def test_integration_rejects_wrong_identity_and_query_plans(change):
    data = {"schema_version": "news-3.0", "client_ref": "new-client", "portfolio_id": 987,
            "status": "no_results", "articles": []}
    data.update(change)
    with pytest.raises(ValueError):
        news_block(data, client_ref="new-client", portfolio_id=987)


def test_legacy_news_results_remain_supported():
    data = {"schema_version": "news-2.0", "client_ref": "new-client", "portfolio_id": 987,
            "status": "no_results", "articles": []}
    before = copy.deepcopy(data)
    assert news_block(data, client_ref="new-client", portfolio_id=987) is data
    assert data == before


@pytest.mark.parametrize("block", [
    {"top_holdings": {"holdings": "bad"}},
    {"allocation": {"allocation": []}},
    {"allocation": {"allocation": {"industry": {"weights": []}}}},
    {"lookthrough": {"dimensions": {"industry": [{"category": []}]}}},
    {"accounts_by_currency": {"by_currency": [{"currency": []}]}},
])
def test_malformed_external_briefing_fails_before_network(block):
    d = dossier()
    payload = {"profile": {"client_ref": d.client_ref, "portfolio_id": d.portfolio_id,
                           "portfolio_currency": d.portfolio.portfolio_currency}, **block}
    with pytest.raises(ValueError):
        context_from_briefing(payload, d)


def test_isin_alone_cannot_resolve_duplicate_instrument_identity():
    d = dossier([{"SecurityId": 1, "SecurityName": "Nestle", "Isin": "CH0038863350",
                  "TotalAmountInPortfolioCurrency": 500}])
    result = collect_news(d, FakeProvider([
        article(title="CH0038863350 update", snippet="", url="https://news.example/id-only"),
    ]), as_of=NOW)
    assert not result.articles


def test_crypto_breed_name_is_not_financial_news():
    from processing.contracts import AccountPosition, Presence, SourceList
    d = dossier([])
    d.account_positions = SourceList([AccountPosition("private", "SHIB", 100, None, None, None)], Presence.PRESENT)
    result = collect_news(d, FakeProvider([
        article(title="Shiba Inu wins dog show", snippet="", url="https://news.example/dog"),
        article(title="Shiba Inu token staking update", snippet="", url="https://news.example/crypto"),
    ]), as_of=NOW)
    assert [a.url for a in result.articles] == ["https://news.example/crypto"]


def test_fixture_results_cannot_enter_production_briefing():
    data = {"schema_version": "news-3.0", "status": "ok", "articles": [], "is_fixture": True}
    with pytest.raises(ValueError, match="Fixture"):
        news_block(data)


def test_small_crypto_does_not_outrank_large_fund_with_mixed_weight_bases(store):
    d = build_dossier(store, "CASE-002")
    ctx = build_news_context(store, d)
    plan = plan_news(d, context=ctx)
    crypto = next(q for q in plan.queries + plan.deferred_queries if q.kind == "crypto")
    row = next(r for r in ctx.blocks["accounts_by_currency"]["by_currency"] if r["category"] == "crypto")
    assert crypto.priority == row["weight_of_total"]
    assert any(e.dimension == "industry" for q in plan.queries for e in q.exposures)


def test_context_mismatch_blocks_provider_call():
    d = dossier()
    ctx = cash_context(d)
    ctx.portfolio_id = 999
    provider = FakeProvider()
    with pytest.raises(ValueError):
        collect_news(d, provider, context=ctx, as_of=NOW)
    assert not provider.calls


def test_market_search_never_invents_subsector():
    d = dossier([])
    ctx = NewsContext(d.client_ref, d.portfolio_id, "CHF", {"allocation": {
        "status": "calculated", "allocation": {"industry": {
            "weights": {"Information Technology": 0.7}, "basis": "all positions",
        }},
    }})
    plan = plan_news(d, context=ctx)
    assert "semiconductors" not in " ".join(q.text for q in plan.queries)


def test_ai_consumes_attached_news_without_a_second_news_provider():
    import ai_engine

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text="test only")

    fake = SimpleNamespace(responses=SimpleNamespace(create=create))
    payload = {"market_context": {"status": "no_results", "articles": []}}
    assert ai_engine.generate_wealth_briefing(payload, openai_client=fake) == "test only"
    assert len(calls) == 1
    assert "market_context" in calls[0]["input"]
    assert "supplied article URL" in calls[0]["instructions"]
    assert not hasattr(ai_engine, "fetch_holding_news")


def test_cli_direct_only_retains_legacy_plan():
    import subprocess
    run = subprocess.run([sys.executable, "-B", "-m", "news", "CASE-016", "--plan", "--direct-only"],
                         cwd=ROOT, capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert len(json.loads(run.stdout)["plan"]["queries"]) == 1


def test_audit_cli_never_overwrites_data():
    import subprocess
    run = subprocess.run([sys.executable, "-B", "-m", "news.audit", "--plan", "--output", "clients.json"],
                         cwd=ROOT, capture_output=True, text=True, check=False)
    assert run.returncode == 2
