from types import SimpleNamespace
from typing import Any

import pytest

from ai_engine import (
    BriefingGenerationError,
    build_briefing_prompt,
    generate_wealth_briefing,
)


class FakeResponses:
    def __init__(self, text="Briefing text"):
        self.text = text
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_text=self.text)


def test_combined_payload_is_sent_to_responses_api():
    responses = FakeResponses()
    client = SimpleNamespace(responses=responses)
    payload = {
        "profile": {"client_ref": "CASE-002"},
        "performance": {"status": "calculated"},
        "market_context": {
            "status": "ok",
            "articles": [{"title": "Held company update"}],
        },
    }

    result = generate_wealth_briefing(payload, openai_client=client)

    assert result == "Briefing text"
    assert responses.kwargs["store"] is False
    assert responses.kwargs["model"] == "gpt-5-mini"
    assert "Held company update" in responses.kwargs["input"]
    assert "calculate new financial metrics" in responses.kwargs["instructions"]


def test_prompt_rejects_non_standard_numbers():
    with pytest.raises(ValueError):
        build_briefing_prompt({"metric": float("nan")})


def test_unavailable_payload_never_calls_model():
    with pytest.raises(BriefingGenerationError, match="not found"):
        generate_wealth_briefing(
            {"status": "unavailable", "reason": "Client not found"},
            openai_client=SimpleNamespace(),
        )
