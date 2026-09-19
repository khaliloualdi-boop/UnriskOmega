"""Tests for the split AI functions (describe_development / advise_from_news)."""
from types import SimpleNamespace
from typing import Any

import pytest

from ai_engine import (
    BriefingGenerationError,
    advise_from_news,
    describe_development,
)


class Capture:
    def __init__(self, text="narration"):
        self.text = text
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_text=self.text)


def _client(cap):
    return SimpleNamespace(responses=cap)


def test_describe_development_sends_budget_and_reasoning():
    cap = Capture()
    out = describe_development({"performance": {"status": "calculated"}}, openai_client=_client(cap))
    assert out == "narration"
    assert cap.kwargs["max_output_tokens"] == 3000
    assert cap.kwargs["reasoning"] == {"effort": "low"}
    assert cap.kwargs["store"] is False


def test_describe_development_without_news_forbids_causes():
    cap = Capture()
    describe_development({"performance": {"status": "calculated"}}, openai_client=_client(cap))
    assert "cannot be determined from the data alone" in cap.kwargs["instructions"]
    assert "EVENTS_IN_DRAWDOWN_WINDOW" not in cap.kwargs["input"]


def test_describe_development_with_period_news_allows_sourced_hypotheses():
    cap = Capture()
    news = [{"headline": "Sector selloff", "publisher": "Reuters", "published_at": "2022-06-01"}]
    describe_development({"performance": {"status": "calculated"}},
                         period_news=news, openai_client=_client(cap))
    assert "hypothesis" in cap.kwargs["instructions"]
    assert "EVENTS_IN_DRAWDOWN_WINDOW" in cap.kwargs["input"]
    assert "Sector selloff" in cap.kwargs["input"]


def test_advise_from_news_runs_and_frames_for_review():
    cap = Capture()
    advise_from_news({"market_context": {"status": "ok"}}, openai_client=_client(cap))
    assert "for the advisor to review" in cap.kwargs["instructions"]


def test_sections_reject_unavailable_payload():
    for fn in (describe_development, advise_from_news):
        with pytest.raises(BriefingGenerationError, match="not found"):
            fn({"status": "unavailable", "reason": "Client not found"}, openai_client=SimpleNamespace())
