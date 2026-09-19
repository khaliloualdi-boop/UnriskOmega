"""Analytics layer for unriskOmega.

Standalone, dependency-free metric functions computed from the typed
data model in `processing`. Each module returns plain JSON-compatible
dicts so the output can be (a) fed to an LLM as a structured payload and
(b) rendered into a report. Nothing here does I/O or network calls.

Modules
-------
performance : return / risk metrics from a portfolio's NAV history.
"""
from .performance import analyze_performance, performance_from_history

__all__ = ["analyze_performance", "performance_from_history"]
