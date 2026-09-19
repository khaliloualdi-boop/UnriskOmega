"""Offline tests: actual ClientDossier integration and fake Apify transport."""
import copy
import json
import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from news.apify import _HTTP, ApifyNewsProvider
from news.models import NewsBatch, NewsProviderError
from news.queries import build_queries
from news.service import (
    canonical_url,
    collect_news,
    parse_publication,
    render_news_context,
)
from processing.contracts import to_jsonable
from processing.dossier import build_all, build_dossier
from processing.loader import load_store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def dossier(positions=None, *, reference=True):
    if positions is None:
        positions = [
            {"SecurityId": 1, "SecurityName": "Namen-Aktie Nestlé SA", "TotalAmountInPortfolioCurrency": 900},
            {"SecurityId": 2, "SecurityName": "Microsoft Inc.", "TotalAmountInPortfolioCurrency": 100},
        ]
    client = {
        "ClientRef": "PRIVATE-CLIENT", "FirstName": "PrivateName",
        "ClientNotes": [{"Note": "Private medical and family information"}],
        "ReportingCurrency": "CHF",
        "Portfolios": [{
            "PortfolioId": 1, "PortfolioCurrency": "CHF",
            "AssetsUnderManagementInDefaultCurrency": 1000,
            "SecurityPositions": positions, "AccountPositions": [],
        }],
    }
    refs = {"Securities": [
        {"Id": 1, "Name": "Namen-Aktie Nestlé SA", "SAA_AssetClassName": "Shares"},
        {"Id": 2, "Name": "Microsoft Inc.", "SAA_AssetClassName": "Shares"},
    ]}
    store = load_store([client], refs if reference else None)
    return build_dossier(store, "PRIVATE-CLIENT", 1, analysis_date=NOW.date())


def article(**changes):
    row = {
        "title": "Nestle reports quarterly results", "source": "Example News",
        "url": "https://news.example/story?id=42&utm_source=feed",
        "snippet": "Nestle published its earnings update.",
        "publishedAt": "2 hours ago", "scrapedAt": NOW.isoformat(), "isSponsored": False,
    }
    row.update(changes)
    return row


class FakeProvider:
    name = "fake"
    is_fixture = True

    def __init__(self, rows=None, fail_first=False):
        self.rows = rows if rows is not None else [article()]
        self.calls = []
        self.fail_first = fail_first

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise NewsProviderError("Simulated service failure.")
        return NewsBatch(copy.deepcopy(self.rows), NOW, "testRun")


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class QueryTests(unittest.TestCase):
    def test_reads_real_contract_and_sends_public_terms_only(self):
        provider = FakeProvider()
        collect_news(dossier(), provider, as_of=NOW)
        wire = json.dumps(provider.calls)
        self.assertIn("Nestl", wire)
        for forbidden in ("PrivateName", "PRIVATE-CLIENT", "medical", "900", "1000", "security_id"):
            self.assertNotIn(forbidden, wire)

    def test_holdings_ranked_by_value_without_asset_fallback(self):
        d = dossier()
        plan = build_queries(d)
        self.assertEqual(plan.queries[0].match_terms, ("Nestlé",))
        self.assertEqual(plan.queries[1].security_ids, (2,))
        self.assertEqual(len(plan.queries), 2)
        self.assertFalse(any(q.kind == "asset_class" for q in plan.queries))
        self.assertLessEqual(len(plan.queries), 4)

    def test_no_reference_still_uses_position_names(self):
        plan = build_queries(dossier(reference=False))
        self.assertEqual(len(plan.queries), 2)
        self.assertEqual(plan.queries[0].match_terms, ("Nestlé",))

    def test_null_positions_are_not_confirmed_cash_only(self):
        d = dossier(positions=[])
        d.security_positions = type(d.security_positions).absent()
        provider = FakeProvider()
        result = collect_news(d, provider, as_of=NOW)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(provider.calls, [])

    def test_unknown_values_do_not_become_zero_or_top_holdings(self):
        plan = build_queries(dossier([{"SecurityId": 1, "SecurityName": "Nestle"}]))
        self.assertEqual(plan.queries, [])
        self.assertTrue(plan.skipped)

    def test_crypto_account_is_not_cash_query(self):
        from processing.contracts import AccountPosition, Presence, SourceList
        d = dossier([])
        d.account_positions = SourceList([
            AccountPosition("Private account", "BTC", 500, None, None, None),
        ], Presence.PRESENT)
        plan = build_queries(d)
        self.assertEqual(plan.queries[0].kind, "crypto")
        self.assertEqual(plan.queries[0].match_terms, ("Bitcoin",))

    def test_alias_overrides_public_instrument_name(self):
        plan = build_queries(dossier(), aliases={1: "Nestle"})
        self.assertEqual(plan.queries[0].match_terms, ("Nestle",))

    def test_actual_export_all_57_portfolios(self):
        clients = json.loads((ROOT / "clients.json").read_text())
        reference = json.loads((ROOT / "reference.json").read_text())
        store = load_store(clients, reference)
        count = 0
        for d in build_all(store, analysis_date=NOW.date()):
            before = to_jsonable(d)
            result = collect_news(d, FakeProvider([]), as_of=NOW)
            self.assertLessEqual(len(result.queries), 4)
            self.assertEqual(before, to_jsonable(d), "News must not mutate shared data.")
            json.dumps(to_jsonable(result), allow_nan=False)
            count += 1
        self.assertEqual(count, 57)


class SelectionTests(unittest.TestCase):
    def run_rows(self, rows):
        return collect_news(dossier(), FakeProvider(rows), as_of=NOW)

    def test_rejects_old_future_unknown_and_unrelated(self):
        result = self.run_rows([
            article(publishedAt="2020-01-01"), article(publishedAt="2026-09-20"),
            article(publishedAt=None), article(title="Sports final", snippet="A football team won."),
            article(isSponsored=True), article(url="javascript:alert(1)"),
        ])
        self.assertFalse(result.articles)
        self.assertEqual(result.status, "no_results")
        self.assertIn("unknown_publication_date", result.rejected_counts)
        self.assertIn("no_direct_holding_match", result.rejected_counts)

    def test_max_three_and_duplicate_urls_titles(self):
        rows = [
            article(),
            article(url="https://news.example/story?id=42&utm_source=other#top"),
            article(url="https://elsewhere.example/copied"),
            *[article(title=f"Nestle financial update {i}", url=f"https://news.example/{i}") for i in range(5)],
        ]
        result = self.run_rows(rows)
        self.assertEqual(len(result.articles), 3)
        self.assertGreater(result.rejected_counts["duplicate"], 0)
        self.assertEqual(len({a.url for a in result.articles}), 3)

    def test_relative_date_uses_scrape_time_and_is_labelled(self):
        result = self.run_rows([article(scrapedAt=(NOW - timedelta(days=2)).isoformat())])
        self.assertEqual(result.articles[0].published_at, NOW - timedelta(days=2, hours=2))
        self.assertTrue(result.articles[0].date_is_estimated)
        self.assertEqual(result.articles[0].published_at_raw, "2 hours ago")

    def test_absolute_dates_preserve_timezone(self):
        value, estimated = parse_publication("2026-09-19T13:00:00+02:00", NOW)
        self.assertEqual(value.hour, 11)
        self.assertFalse(estimated)

    def test_expired_scraped_results_cannot_look_fresh(self):
        result = self.run_rows([article(scrapedAt="2025-01-01T00:00:00Z")])
        self.assertFalse(result.articles)

    def test_unknown_and_overflowing_dates_do_not_crash(self):
        for value in ("N/A", "some time ago", "9" * 400 + " years ago", {}, None):
            self.assertIsNone(parse_publication(value, NOW)[0])

    def test_partial_provider_failure_retains_successful_articles(self):
        rows = [article(title="Microsoft earnings report", snippet="Microsoft results.")]
        result = collect_news(dossier(), FakeProvider(rows, fail_first=True), as_of=NOW)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.failed_queries, 1)
        self.assertEqual(len(result.articles), 1)

    def test_total_failure_is_not_no_results(self):
        class Failing(FakeProvider):
            def search(self, query, **kwargs):
                raise NewsProviderError("Unavailable")
        result = collect_news(dossier(), Failing(), as_of=NOW)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.articles)

    def test_evidence_and_compact_context(self):
        result = self.run_rows([article()])
        a = result.articles[0]
        self.assertEqual(a.matches[0].holdings[0].security_id, 1)
        self.assertTrue(a.matches[0].evidence)
        self.assertEqual(a.matches[0].source_excerpt, a.title)
        context = json.loads(render_news_context(result))
        self.assertNotIn("relevance_reason", context["articles"][0])
        self.assertEqual(context["articles"][0]["matches"][0]["holdings"][0]["position_value"], 900)
        self.assertTrue(result.is_fixture)

    def test_missing_title_publisher_and_malformed_rows(self):
        result = self.run_rows([article(title=None), article(source=None), 123, {"error": "failure"}])
        self.assertFalse(result.articles)

    def test_urls_keep_identifiers(self):
        self.assertEqual(canonical_url("https://news.example/x?id=42&utm_medium=x#top"),
                         "https://news.example/x?id=42")
        self.assertIsNone(canonical_url("https://user:pass@news.example/x"))

    def test_recent_direct_holding_ranked_above_general_market(self):
        result = self.run_rows([
            article(title="Equity markets rally", snippet="Stocks rise.", url="https://news.example/macro"),
            article(),
        ])
        self.assertEqual(result.articles[0].matches[0].term, "Nestlé")
        self.assertEqual(len(result.articles), 1)

    def test_substring_false_positive_is_excluded(self):
        result = collect_news(dossier(), FakeProvider([
            article(title="Education news", snippet="School funding."),
        ]), as_of=NOW, aliases={1: "CAT"})
        self.assertFalse(result.articles)

    def test_invalid_output_limits_are_rejected_before_network(self):
        provider = FakeProvider()
        with self.assertRaises(ValueError):
            collect_news(dossier(), provider, max_articles=4)
        self.assertFalse(provider.calls)


class ApifyTests(unittest.TestCase):
    def test_runs_once_polls_and_reads_bounded_dataset(self):
        http = FakeHTTP([
            {"data": {"id": "run123", "status": "RUNNING"}},
            {"data": {"id": "run123", "status": "SUCCEEDED", "defaultDatasetId": "data123"}},
            [article()],
        ])
        batch = ApifyNewsProvider(transport=http).search("Nestle", limit=10, lookback_days=7)
        self.assertEqual(batch.run_id, "run123")
        self.assertEqual([call[0] for call in http.calls], ["POST", "GET", "GET"])
        self.assertIn("maxTotalChargeUsd=0.05", http.calls[0][1])
        self.assertEqual(http.calls[0][2]["query"], "Nestle")
        self.assertEqual(http.calls[0][2]["timeRange"], "7d")
        self.assertIn("limit=10", http.calls[-1][1])

    def test_post_failure_is_not_retried(self):
        http = FakeHTTP([NewsProviderError("Timeout")])
        with self.assertRaises(NewsProviderError):
            ApifyNewsProvider(transport=http).search("Nestle")
        self.assertEqual(len(http.calls), 1)

    def test_failed_run_does_not_read_partial_dataset(self):
        http = FakeHTTP([{"data": {"id": "r1", "status": "FAILED"}}])
        with self.assertRaises(NewsProviderError):
            ApifyNewsProvider(transport=http).search("Nestle")
        self.assertEqual(len(http.calls), 1)

    def test_missing_token_is_actionable(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(NewsProviderError, "APIFY_TOKEN"),
        ):
            ApifyNewsProvider()

    def test_credentials_not_in_url_or_errors(self):
        from urllib.error import HTTPError
        http = _HTTP("secret-token")
        with (
            patch.object(
                http._opener,
                "open",
                side_effect=HTTPError(
                    "https://secret-token", 401, "secret-token", Message(), None,
                ),
            ) as opener,
            self.assertRaises(NewsProviderError) as exc,
        ):
            http.request("GET", "actor-runs/r1")
        self.assertNotIn("secret-token", str(exc.exception))
        request = opener.call_args.args[0]
        self.assertNotIn("secret-token", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")

    def test_invalid_dataset_is_reported(self):
        http = FakeHTTP([
            {"data": {"id": "r1", "status": "SUCCEEDED", "defaultDatasetId": "d1"}}, {},
        ])
        with self.assertRaises(NewsProviderError):
            ApifyNewsProvider(transport=http).search("Nestle")

    def test_resource_ids_cannot_change_endpoint(self):
        http = FakeHTTP([{"data": {"id": "../users", "status": "RUNNING"}}])
        with self.assertRaises(NewsProviderError):
            ApifyNewsProvider(transport=http).search("Nestle")

    def test_cli_plan_uses_actual_holdings_without_articles(self):
        run = subprocess.run(
            [sys.executable, "-B", "-m", "news", "CASE-016", "--plan"],
            cwd=ROOT, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        data = json.loads(run.stdout)
        self.assertEqual(data["mode"], "query_plan")
        self.assertNotIn("articles", data)
        self.assertEqual(len(data["plan"]["queries"]), 4)
        self.assertIn("VZ Holding", data["plan"]["queries"][0]["text"])


class StrictPersonalizationTests(unittest.TestCase):
    def test_rejects_three_real_irrelevant_case016_results(self):
        # Headlines observed in the user's actual Apify output, previously admitted
        # by the broad "equity markets" query. Dates controlled for repeatability.
        titles = [
            "Best Buy's Chairman Emeritus Sells 300,000 Shares for $27.8 Million as the Stock...",
            "Alexandria Real Estate Equities Inc (ARE) Shares Fall 5.4% -- Wh",
            "InMed Pharmaceuticals (INM) Faces Nasdaq Equity Shortfall Amid I",
        ]
        clients = json.loads((ROOT / "clients.json").read_text())
        reference = json.loads((ROOT / "reference.json").read_text())
        d = build_dossier(load_store(clients, reference), "CASE-016", analysis_date=NOW.date())
        rows = [article(title=title, snippet="", url=f"https://news.example/{i}") for i, title in enumerate(titles)]
        result = collect_news(d, FakeProvider(rows), as_of=NOW)
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.rejected_counts["no_direct_holding_match"], 3)
        self.assertFalse(result.articles)
        self.assertEqual(len(result.queries), 1)
        self.assertEqual(result.queries[0].holdings[0].security_id, 18477)

    def test_same_candidates_give_different_results_for_different_holdings(self):
        rows = [
            article(title="Nestle earnings increase", snippet="Nestle", url="https://news.example/n"),
            article(title="Microsoft announces acquisition", snippet="Microsoft", url="https://news.example/m"),
        ]
        first = dossier([{"SecurityId": 1, "SecurityName": "Nestle", "TotalAmountInPortfolioCurrency": 900}])
        second = dossier([{"SecurityId": 2, "SecurityName": "Microsoft", "TotalAmountInPortfolioCurrency": 500}])
        a = collect_news(first, FakeProvider(rows), as_of=NOW)
        b = collect_news(second, FakeProvider(rows), as_of=NOW)
        self.assertEqual([x.url for x in a.articles], ["https://news.example/n"])
        self.assertEqual([x.url for x in b.articles], ["https://news.example/m"])

    def test_generic_words_are_not_enough_even_when_provider_returns_them(self):
        result = collect_news(dossier(), FakeProvider([
            article(title="Stocks rally as bond yields fall", snippet="Equity markets and financial news."),
        ]), as_of=NOW)
        self.assertFalse(result.articles)

    def test_ambiguous_company_name_requires_business_context(self):
        d = dossier([{"SecurityId": 3, "SecurityName": "Apple Inc.", "TotalAmountInPortfolioCurrency": 20}])
        result = collect_news(d, FakeProvider([
            article(title="Apple harvest expected next week", snippet="", url="https://news.example/farm"),
            article(title="Apple earnings exceed expectations", snippet="", url="https://news.example/earnings"),
        ]), as_of=NOW)
        self.assertEqual([a.url for a in result.articles], ["https://news.example/earnings"])

    def test_fund_manager_alone_is_not_the_held_fund(self):
        d = dossier([{
            "SecurityId": 8, "SecurityName": "Anteile -USD- iShares Edge MSCI USA Quality Factor UCITS ETF",
            "TotalAmountInPortfolioCurrency": 500,
        }], reference=False)
        rows = [
            article(title="iShares announces new products", snippet="", url="https://news.example/manager"),
            article(title="iShares Edge MSCI USA Quality Factor UCITS ETF changes fees",
                    snippet="", url="https://news.example/fund"),
        ]
        result = collect_news(d, FakeProvider(rows), as_of=NOW)
        self.assertEqual([a.url for a in result.articles], ["https://news.example/fund"])
        self.assertEqual(result.articles[0].matches[0].holdings[0].relation, "fund")

    def test_bond_name_resolves_issuer_and_retains_bond_identity(self):
        d = dossier([{
            "SecurityId": 9, "SecurityName": "1.125 % Clariant AG 2019-30.01.29",
            "TotalAmountInPortfolioCurrency": 70,
        }], reference=False)
        plan = build_queries(d)
        self.assertEqual(plan.queries[0].text, '"Clariant"')
        result = collect_news(d, FakeProvider([article(title="Clariant announces earnings", snippet="")]), as_of=NOW)
        match = result.articles[0].matches[0]
        self.assertEqual(match.holdings[0].relation, "bond_issuer")
        self.assertIn("2019-30.01.29", match.holdings[0].instrument_name)

    def test_multiple_bonds_of_same_issuer_keep_all_security_ids(self):
        d = dossier([
            {"SecurityId": 8, "SecurityName": "1.1 % Clariant AG 2019-30.01.29", "TotalAmountInPortfolioCurrency": 80},
            {"SecurityId": 9, "SecurityName": "1.2 % Clariant AG 2020-30.01.30", "TotalAmountInPortfolioCurrency": 20},
        ], reference=False)
        plan = build_queries(d)
        self.assertEqual(len(plan.queries), 1)
        self.assertEqual(set(plan.queries[0].security_ids), {8, 9})

    def test_all_deferred_exposures_remain_visible(self):
        d = dossier([
            {"SecurityId": i, "SecurityName": f"Company {i} AG", "TotalAmountInPortfolioCurrency": i * 100}
            for i in range(5, 11)
        ], reference=False)
        plan = build_queries(d, max_queries=2)
        self.assertEqual(len(plan.queries), 2)
        self.assertEqual(len(plan.deferred_queries), 4)
        self.assertEqual(plan.queries[0].security_ids, (10,))

    def test_cash_does_not_trigger_generic_fx_search(self):
        from processing.contracts import AccountPosition, Presence, SourceList
        d = dossier([])
        d.account_positions = SourceList([
            AccountPosition("Private account", "CHF", 500, None, None, None),
        ], Presence.PRESENT)
        result = collect_news(d, FakeProvider(), as_of=NOW)
        self.assertFalse(result.queries)
        self.assertEqual(result.status, "unavailable")

    def test_generic_alias_is_rejected(self):
        d = dossier([{"SecurityId": 1, "SecurityName": "Nestle", "TotalAmountInPortfolioCurrency": 80}])
        self.assertFalse(build_queries(d, aliases={1: "equity markets"}).queries)

    def test_article_spanning_two_entities_retains_both_links(self):
        result = collect_news(dossier(), FakeProvider([
            article(title="Nestle signs Microsoft agreement", snippet=""),
        ]), as_of=NOW)
        ids = {h.security_id for m in result.articles[0].matches for h in m.holdings}
        self.assertEqual(ids, {1, 2})
        self.assertEqual(len(result.articles), 1)

    def test_diversity_covers_another_held_entity_before_repeating(self):
        result = collect_news(dossier(), FakeProvider([
            article(title=f"Nestle earnings update {i}", url=f"https://news.example/{i}") for i in range(3)
        ] + [article(title="Microsoft earnings", snippet="", url="https://news.example/ms")]), as_of=NOW, max_articles=2)
        self.assertEqual(len(result.articles), 2)
        ids = {h.security_id for a in result.articles for m in a.matches for h in m.holdings}
        self.assertEqual(ids, {1, 2})

    def test_cli_live_missing_token_does_not_create_fake_articles(self):
        env = {k: v for k, v in os.environ.items() if k != "APIFY_TOKEN"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        run = subprocess.run(
            [sys.executable, "-B", "-m", "news", "CASE-016", "--live"],
            cwd=ROOT, capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("APIFY_TOKEN", run.stderr)
        self.assertEqual(run.stdout, "")

    def test_cli_requires_explicit_mode_and_preserves_existing_output(self):
        for flags in ([], ["--plan", "--output", "clients.json"]):
            run = subprocess.run(
                [sys.executable, "-B", "-m", "news", "CASE-016", *flags],
                cwd=ROOT, capture_output=True, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
            self.assertEqual(run.returncode, 2)

    def test_partial_failure_query_is_identified(self):
        result = collect_news(dossier(), FakeProvider(fail_first=True), as_of=NOW)
        self.assertEqual(result.status, "partial")
        self.assertTrue(any('query="Nestlé"' in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
