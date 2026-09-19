"""Select source articles with explicit matches to named portfolio exposures."""
import json
import re
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from processing.contracts import ClientDossier, SourceRef, to_jsonable

from .models import ArticleMatch, NewsArticle, NewsProviderError, NewsResult
from .queries import AMBIGUOUS_NAMES, build_queries, normalize

BUSINESS_TERMS = (
    "stock", "stocks", "shares", "shareholders", "earnings", "dividend", "dividends",
    "revenue", "bonds", "bond", "investor", "investors", "quarterly results",
    "acquisition", "merger", "takeover", "bankruptcy", "nasdaq", "nyse",
    "lawsuit", "recall", "regulatory", "antitrust", "profit", "profits",
    "restructuring", "layoffs", "ceo", "valuation", "cash flow",
)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Expected a timezone-aware datetime.")
    return value.astimezone(UTC)


def _text(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        if any(c.isspace() for c in value):
            return None
        query = [
            (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
        ]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                           urlencode(sorted(query)), ""))
    except ValueError:
        return None


def parse_publication(value, anchor):
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    relative = re.fullmatch(
        r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago",
        text,
        flags=re.IGNORECASE,
    )
    if relative:
        days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}
        try:
            return anchor - timedelta(days=int(relative[1]) * days[relative[2].lower()]), True
        except (ValueError, OverflowError):
            return None, False
    if text.lower() in {"just now", "today", "yesterday"}:
        return anchor - timedelta(days=int(text.lower() == "yesterday")), True
    try:
        parsed = datetime.fromisoformat(text)
        estimated = parsed.tzinfo is None or len(text) == 10
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC), estimated
    except (ValueError, OverflowError):
        return None, False


def _contains(text, term):
    needle = normalize(term)
    return bool(needle) and f" {needle} " in f" {text} "


def _matches(title, snippet, queries, provider, url):
    """Keep the actual field that supports each entity match, not a stock explanation."""
    matches = []
    priorities = []
    text = normalize(title + " " + snippet)
    for query in queries:
        for term in query.match_terms:
            field = "title" if _contains(normalize(title), term) else (
                "snippet" if _contains(normalize(snippet), term) else None
            )
            if field is None:
                continue
            # Common dictionary/company names need a business context.
            if normalize(term) in AMBIGUOUS_NAMES and not any(_contains(text, word) for word in BUSINESS_TERMS):
                continue
            excerpt = title if field == "title" else snippet
            matches.append(ArticleMatch(
                term=term, field=field, source_excerpt=excerpt,
                holdings=list(query.holdings),
                evidence=list(query.evidence) + [
                    SourceRef(source=provider, path=url, field=field, value=excerpt),
                ],
            ))
            priorities.append(query.priority)
    return matches, max(priorities, default=0.0)


def _identities(article):
    return {
        (h.security_id, h.source_paths)
        for match in article.matches for h in match.holdings
    }


def _search_batches(queries, provider, limit, lookback_days, budget, workers, progress):
    """Bound caller waiting, including providers that do not honour timeouts."""
    deadline = time.monotonic() + budget
    pool = ThreadPoolExecutor(max_workers=workers)

    def search(query):
        if time.monotonic() >= deadline:
            raise NewsProviderError("News search time limit reached.")
        # Apify supports a shared deadline; other provider contracts remain valid.
        from .apify import ApifyNewsProvider
        options = {"deadline": deadline} if isinstance(provider, ApifyNewsProvider) else {}
        return provider.search(query.text, limit=limit, lookback_days=lookback_days, **options)

    pending = {pool.submit(search, query): index for index, query in enumerate(queries)}
    outcomes = {}
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                index = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except NewsProviderError as exc:
                    outcomes[index] = exc
                progress(f"Searching news: {len(outcomes)} of {len(queries)} completed…")
        for future, index in pending.items():
            future.cancel()
            outcomes[index] = NewsProviderError("News search time limit reached; coverage is incomplete.")
        if pending:
            progress("News time limit reached. Continuing with available results…")
    finally:
        # Waiting here would defeat the overall deadline. Live requests have
        # their own short timeouts and remote actor runtime limits.
        pool.shutdown(wait=False, cancel_futures=True)
    return [outcomes[index] for index in range(len(queries))]


def collect_news(
    dossier: ClientDossier, provider, *, as_of=None, lookback_days=7,
    max_articles=3, per_query_limit=10, aliases=None, max_queries=4,
    time_budget=60, max_workers=1, progress=None,
) -> NewsResult:
    """Zero results are valid: no broad-market fallback or synthesized article."""
    now = _utc(as_of or datetime.now(UTC))
    if not 0 < time_budget <= 120 or max_workers not in (1, 2):
        raise ValueError("Use a positive news budget up to 120 seconds and one or two workers.")
    for value, lower, upper in ((max_articles, 1, 3), (lookback_days, 1, 30), (per_query_limit, 1, 20)):
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError("Use 1–3 articles, 1–30 days and 1–20 candidates per query.")
    plan = build_queries(dossier, aliases=aliases, max_queries=max_queries)
    result = NewsResult(
        client_ref=dossier.client_ref, portfolio_id=dossier.portfolio_id, as_of=now,
        provider=provider.name, is_fixture=provider.is_fixture,
        status="unavailable" if not plan.queries else "no_results",
        queries=plan.queries, warnings=list(plan.warnings),
        skipped=plan.skipped, deferred_queries=plan.deferred_queries,
        portfolio_snapshot_at=dossier.portfolio.factory_date_utc,
        history_as_of=dossier.data_as_of.isoformat() if dossier.data_as_of else None,
    )
    if dossier.analysis_date != now.date():
        result.warnings.append(f"analysis_date={dossier.analysis_date}; news_cutoff={now.date()}")
    rejects = Counter()
    candidates = []
    batches = _search_batches(plan.queries, provider, per_query_limit, lookback_days,
                              time_budget, max_workers, progress or (lambda message: None))
    for query, batch in zip(plan.queries, batches, strict=True):
        if isinstance(batch, NewsProviderError):
            result.failed_queries += 1
            result.warnings.append(f"query={query.text}: {batch}")
            continue
        retrieved = _utc(batch.retrieved_at)
        for row in batch.items[:per_query_limit]:
            if not isinstance(row, dict) or row.get("error"):
                rejects["invalid_record"] += 1
                continue
            if row.get("isSponsored") not in (False, None):
                rejects["sponsored"] += 1
                continue
            title = _text(row.get("title"), 240)
            snippet = _text(row.get("snippet"), 500)
            publisher = _text(row.get("source"), 120)
            url = canonical_url(row.get("url"))
            if not title or not publisher or not url:
                rejects["missing_metadata"] += 1
                continue
            scraped, _ = parse_publication(row.get("scrapedAt"), retrieved)
            if not isinstance(row.get("scrapedAt"), str) or not re.match(r"^\d{4}-", row["scrapedAt"]):
                scraped = None
            if scraped and scraped > retrieved + timedelta(minutes=5):
                rejects["invalid_scrape_date"] += 1
                continue
            published, estimated = parse_publication(row.get("publishedAt"), scraped or retrieved)
            if published is None:
                rejects["unknown_publication_date"] += 1
                continue
            if published < now - timedelta(days=lookback_days) or published > now + timedelta(minutes=5):
                rejects["outside_time_window"] += 1
                continue
            matches, priority = _matches(title, snippet, plan.queries, provider.name, url)
            if not matches:
                rejects["no_direct_holding_match"] += 1
                continue
            age = max(0, (now - published).total_seconds() / 86400)
            components = {
                "entity_in_title_or_snippet": 50.0 if any(m.field == "title" for m in matches) else 30.0,
                "position_value_relative_to_largest_searched": round(30 * priority, 2),
                "recency": round(20 * (1 - age / lookback_days), 2),
            }
            candidates.append(NewsArticle(
                title=title, url=url, publisher=publisher, snippet=snippet,
                published_at=published, published_at_raw=_text(row.get("publishedAt"), 100),
                date_is_estimated=estimated, retrieved_at=retrieved,
                relevance_score=round(sum(components.values()), 2), score_components=components,
                matches=matches, provider_run_ids=[batch.run_id] if batch.run_id else [],
            ))
    candidates.sort(key=lambda a: (-a.relevance_score, -a.published_at.timestamp(), a.url))
    unique, by_url, by_title = [], {}, {}
    for article in candidates:
        title_key = normalize(article.title)
        prior = by_url.get(article.url) or by_title.get(title_key)
        if prior is not None:
            rejects["duplicate"] += 1
            for match in article.matches:
                if match not in prior.matches:
                    prior.matches.append(match)
            prior.provider_run_ids = sorted(set(prior.provider_run_ids + article.provider_run_ids))
            by_url[article.url] = prior
            by_title[title_key] = prior
            continue
        unique.append(article)
        by_url[article.url] = by_title[title_key] = article

    # Cover distinct actual positions first; then fill remaining slots by score.
    covered, selected = set(), []
    for article in unique:
        ids = _identities(article)
        if ids - covered and len(selected) < max_articles:
            selected.append(article)
            covered.update(ids)
    for article in unique:
        if len(selected) >= max_articles:
            break
        if article not in selected:
            selected.append(article)
    result.articles = selected
    result.rejected_counts = dict(rejects)
    if plan.queries:
        if result.failed_queries == len(plan.queries):
            result.status = "unavailable"
        elif result.failed_queries:
            result.status = "partial"
        else:
            result.status = "ok" if result.articles else "no_results"
    return result


def render_news_context(result: NewsResult) -> str:
    """Serialize article data and exact matches; no generated prose or advice."""
    payload = {
        "schema_version": result.schema_version,
        "client_ref": result.client_ref,
        "portfolio_id": result.portfolio_id,
        "status": result.status,
        "is_fixture": result.is_fixture,
        "news_as_of": result.as_of,
        "portfolio_snapshot_at": result.portfolio_snapshot_at,
        "history_as_of": result.history_as_of,
        "articles": [
            {
                "title": a.title, "source": a.publisher, "url": a.url,
                "published_at": a.published_at, "date_is_estimated": a.date_is_estimated,
                "snippet": a.snippet,
                "matches": [
                    {"term": m.term, "field": m.field, "source_excerpt": m.source_excerpt,
                     "holdings": m.holdings}
                    for m in a.matches
                ],
            }
            for a in result.articles
        ],
        "coverage": {"skipped": result.skipped, "deferred_queries": len(result.deferred_queries)},
        "warnings": result.warnings,
    }
    return json.dumps(to_jsonable(payload), ensure_ascii=False, allow_nan=False)
