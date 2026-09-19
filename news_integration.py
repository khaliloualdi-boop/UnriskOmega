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
  * An empty/None bundle leaves the payload identical to a normal run. News can
    never break the core; the worst case is that it adds nothing.
"""

from __future__ import annotations

import json
from typing import Any

from processing.contracts import BriefingPayload
from pipeline import build_briefing_payload

# render_news_context already produces the exact, evidence-preserving, JSON-safe
# shape Sohaib maintains. Reuse it so we inherit any format change he makes.
from news.service import render_news_context

__all__ = [
    "attach_news", "build_payload_with_news", "news_block",
    "preserve_all_findings",
]


def news_block(news_result: Any | None) -> dict[str, Any] | None:
    """Turn a NewsResult into the plain dict the payload carries, or None.

    None in -> None out. Anything already a dict is passed through untouched, so
    a caller that pre-rendered the news can hand it straight in.
    """
    if news_result is None:
        return None
    if isinstance(news_result, dict):
        return news_result
    # render_news_context returns a JSON string; parse it back to a dict so the
    # payload holds structured data, not a string blob.
    return json.loads(render_news_context(news_result))


def attach_news(payload: BriefingPayload, news_result: Any | None) -> BriefingPayload:
    """Set payload.news in place and return it. A None result is a no-op."""
    block = news_block(news_result)
    if block is not None:
        payload.news = block
    return payload


def preserve_all_findings(payload, full_findings=None):
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
    dossier,
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
