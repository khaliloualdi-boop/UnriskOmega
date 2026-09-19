"""Payload-assembly layer for the API: attach news, preserve the full
finding collection. Wraps the pipeline without modifying it.

Owner: Mohamed. This is the M6 external-input adapter, kept deliberately thin.

Why a separate file: the news module (news/) and the analytics pipeline
(pipeline.py) are owned by other people and edited actively. This wrapper calls
both without modifying either, so it cannot collide with their work.

Two rules, both from the M6 spec:
  * No network here. News is COLLECTED elsewhere (news.service.collect_news, which
    takes a provider) and the finished NewsResult is passed in. This keeps
    build_briefing_payload pure and offline, exactly as its docstring promises.
  * None leaves the payload identical to a normal run. Invalid bundles are
    rejected explicitly before they can contaminate another client's briefing.
"""

from __future__ import annotations

import json
from typing import Any

# render_news_context already produces the exact, evidence-preserving, JSON-safe
# shape Sohaib maintains. Reuse it so we inherit any format change he makes.
from news.service import render_news_context
from pipeline import build_briefing_payload
from processing.contracts import BriefingPayload, ClientDossier, Finding

__all__ = [
    "attach_news", "build_payload_with_news", "news_block",
    "preserve_all_findings",
]


def news_block(news_result: Any | None, *, client_ref=None, portfolio_id=None) -> dict[str, Any] | None:
    """Turn a NewsResult into the plain dict the payload carries, or None.

    None in -> None out. A valid dictionary is retained unchanged; identity and
    shape are checked so a query plan or another client's result cannot slip in.
    """
    if news_result is None:
        return None
    if isinstance(news_result, dict):
        parsed = news_result
    # render_news_context returns a JSON string; parse it back to a dict so the
    # payload holds structured data, not a string blob.
    else:
        parsed = json.loads(render_news_context(news_result))
    if not isinstance(parsed, dict):
        raise TypeError("Rendered news context must be a JSON object.")
    if parsed.get("mode") or parsed.get("schema_version") not in {"news-2.0", "news-3.0"}:
        raise ValueError("Expected a news result, not a query plan or an unrelated JSON file.")
    if client_ref is not None and parsed.get("client_ref") != client_ref:
        raise ValueError("News client does not match the briefing client.")
    if portfolio_id is not None and parsed.get("portfolio_id") != portfolio_id:
        raise ValueError("News portfolio does not match the briefing portfolio.")
    if parsed.get("status") not in {"ok", "partial", "no_results", "unavailable"}:
        raise ValueError("Invalid news status.")
    if parsed.get("is_fixture"):
        raise ValueError("Fixture news must not be attached to a production briefing.")
    if not isinstance(parsed.get("articles"), list):
        raise ValueError("News articles must be a list.")  # noqa: TRY004 - invalid external JSON contract
    return parsed


def attach_news(payload: BriefingPayload, news_result: Any | None) -> BriefingPayload:
    """Set payload.news in place and return it. A None result is a no-op."""
    block = news_block(news_result, client_ref=payload.client_ref, portfolio_id=payload.portfolio_id)
    if block is not None:
        payload.news = block
    return payload


def preserve_all_findings(
    payload: BriefingPayload,
    full_findings: list[Finding] | None = None,
) -> BriefingPayload:
    """Guarantee payload.all_findings holds the COMPLETE finding collection.

    Why: selected_findings is only what the 60-second briefing shows. A follow-up
    chatbot (and any evidence lookup) needs everything the analysis produced. This
    keeps the full set in the payload so a later narrowing step can trim
    selected_findings without discarding anything.

    full_findings: the complete list, when the caller has one (e.g. Khalil's
    analytics before it narrows for display). When omitted, all_findings defaults
    to a copy of selected_findings -- correct today, since nothing narrows yet.
    Idempotent and never destructive: an already-populated all_findings is kept.
    """
    if full_findings is not None:
        payload.all_findings = list(full_findings)
    elif not payload.all_findings:
        payload.all_findings = list(payload.selected_findings)
    return payload


def build_payload_with_news(
    dossier: ClientDossier,
    news_result: Any | None = None,
    *,
    largest_threshold: float = 0.10,
    top_five_threshold: float = 0.40,
) -> BriefingPayload:
    """build_briefing_payload, then attach news if any was supplied.

    Drop-in for build_briefing_payload: same result when news_result is None.
    """
    payload = build_briefing_payload(
        dossier,
        largest_threshold=largest_threshold,
        top_five_threshold=top_five_threshold,
    )
    preserve_all_findings(payload)
    return attach_news(payload, news_result)
