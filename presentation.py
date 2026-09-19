"""Display helpers; never modify the source analytics or their precision."""
import re


def narrative_markdown(text: str) -> str:
    """Keep Markdown emphasis while avoiding accidental math rendering.

    Unwrap common text-only LaTeX from older responses. Do not attempt to
    evaluate formulas or rewrite numbers, links, or financial calculations.
    """
    text = re.sub(r"\\(?:text|mathrm|mathbf|textbf)\{([^{}]*)\}", r"\1", text)
    for delimiter in (r"\(", r"\)", r"\[", r"\]"):
        text = text.replace(delimiter, "")
    text = text.replace(r"\%", "%")
    # Preserve currency symbols as literal text, including already escaped ones.
    return re.sub(r"(?<!\\)\$", r"\\$", text)


def news_status_message(context: dict) -> str:
    status = context.get("status")
    if status == "no_results":
        return "Search completed. No articles met the portfolio relevance and date criteria."
    if status == "partial":
        return "Some news searches could not be completed. News coverage is incomplete."
    if status == "ok" and context.get("articles"):
        return "Relevant news is available for this portfolio."
    return "News could not be retrieved for this run. Review the details under Sources and data limitations."
