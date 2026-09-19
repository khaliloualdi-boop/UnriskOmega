from presentation import narrative_markdown, news_status_message


def test_plain_markdown_preserves_facts_and_links():
    text = "**CHF 184,752.64** · -2.3187% [Source](https://example.com/news)"
    assert narrative_markdown(text) == text


def test_common_latex_is_plain_text_and_currency_cannot_start_math():
    assert narrative_markdown(r"\(\text{CHF} 123\) and \[15.0\%\]") == "CHF 123 and 15.0%"
    assert narrative_markdown("$10 and $20") == r"\$10 and \$20"
    assert narrative_markdown(r"\$10") == r"\$10"


def test_news_failure_is_not_reported_as_empty_success():
    assert "Search completed" in news_status_message({"status": "no_results"})
    assert "could not be retrieved" in news_status_message({"status": "unavailable"})
    assert "incomplete" in news_status_message({"status": "partial"})
