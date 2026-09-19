"""Local case discovery and one-click orchestration; no calls on page reruns."""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from ai_engine import (
    BriefingGenerationError,
    advise_from_news,
    describe_development,
    generate_wealth_briefing,
)
from analytics.payload import build_briefing
from news.apify import ApifyNewsProvider
from news.models import NewsProviderError
from news.service import collect_news, render_news_context
from presentation import news_status_message
from processing.dossier import build_dossier
from processing.loader import load_store


def case_files(root: Path) -> list[Path]:
    """Use inbox cases when present, otherwise the bundled client export."""
    files = sorted((root / "inputs").glob("*.json"))
    return files or [root / "clients.json"]


def load_case(path: Path, reference_path: Path):
    """Accept a single client, a client array, or a Clients wrapper.

    Fingerprint the bytes of both inputs so replacement files invalidate results,
    even when client IDs and filenames have not changed.
    """
    raw = path.read_bytes()
    reference = reference_path.read_bytes()
    data = json.loads(raw)
    if isinstance(data, dict) and "ClientRef" in data:
        data = [data]
    if not isinstance(data, list) and not (
        isinstance(data, dict) and isinstance(data.get("Clients"), list)
    ):
        raise TypeError("Expected a client object, a client list, or a Clients wrapper.")
    store = load_store(data, json.loads(reference), source=path.name)
    if not store.clients:
        raise ValueError("No usable clients were found. Each client needs a ClientRef.")
    fingerprint = hashlib.sha256(raw + b"\0" + reference).hexdigest()
    return store, fingerprint


def run_workflow(store, client_ref: str, portfolio_id: int, *,
                 provider_factory=ApifyNewsProvider, collector=collect_news,
                 narrators=None, progress=None, news_cache=None, cache_key=None) -> dict:
    """Run once per button press, preserving useful results on service failure."""
    update = progress or (lambda message: None)
    update("Calculating the portfolio profile…")
    payload = build_briefing(store, portfolio_id, n_paths=2_000)
    if payload.get("status") == "unavailable":
        raise ValueError(payload.get("reason", "Analysis unavailable."))
    dossier = build_dossier(store, client_ref, portfolio_id, analysis_date=datetime.now(UTC).date())
    update("Finding news for this portfolio…")
    errors = {}
    try:
        cached = news_cache.get(cache_key) if news_cache is not None and cache_key is not None else None
        if cached and 0 <= time.monotonic() - cached[0] < 1800:
            update("Using news checked within the last 30 minutes…")
            payload["market_context"] = deepcopy(cached[1])
        else:
            options = {"time_budget": 45, "max_workers": 4, "per_query_limit": 6, "progress": update} if collector is collect_news else {}
            news = collector(dossier, provider_factory(), **options)
            payload["market_context"] = json.loads(render_news_context(news))
            if (news_cache is not None and cache_key is not None
                    and news.status in {"ok", "no_results"}):
                if len(news_cache) >= 32:
                    news_cache.pop(next(iter(news_cache)))
                news_cache[cache_key] = (time.monotonic(), deepcopy(payload["market_context"]))
    except NewsProviderError as exc:
        errors["news"] = str(exc)
        payload["market_context"] = {
            "status": "unavailable", "articles": [], "reason": str(exc),
            "client_ref": client_ref, "portfolio_id": portfolio_id,
        }
    narrators = narrators if narrators is not None else {
        "briefing": generate_wealth_briefing,
        "development": describe_development,
        "news": advise_from_news,
    }
    texts = {}
    for section, narrate in narrators.items():
        if section == "news" and not payload["market_context"].get("articles"):
            texts[section] = news_status_message(payload["market_context"])
            continue
        update(f"Writing {section}…")
        try:
            texts[section] = narrate(payload)
        except BriefingGenerationError as exc:
            errors[section] = str(exc)
    return {"payload": payload, "texts": texts, "errors": errors}
