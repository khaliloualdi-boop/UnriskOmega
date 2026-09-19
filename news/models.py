"""News-only contracts: source articles and verifiable portfolio links."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from processing.contracts import SourceRef


@dataclass(frozen=True)
class HoldingLink:
    security_id: int | None
    instrument_name: str
    position_value: float
    position_currency: str | None
    relation: str
    source_paths: tuple[str, ...]


@dataclass(frozen=True)
class NewsQuery:
    text: str
    kind: str
    match_terms: tuple[str, ...]
    holdings: tuple[HoldingLink, ...]
    evidence: tuple[SourceRef, ...]
    priority: float = 1.0
    exposures: tuple["ExposureLink", ...] = ()
    required_groups: tuple[tuple[str, ...], ...] = ()
    priority_basis: str = "relative_position_value"

    @property
    def security_ids(self):
        return tuple(h.security_id for h in self.holdings if h.security_id is not None)


@dataclass
class QueryPlan:
    queries: list[NewsQuery] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    deferred_queries: list[NewsQuery] = field(default_factory=list)


@dataclass(frozen=True)
class ExposureLink:
    dimension: str
    category: str
    weight: float | None
    basis: str
    source_path: str
    scope: str = "reported_exposure_not_causal_attribution"


@dataclass
class NewsContext:
    client_ref: str
    portfolio_id: int
    portfolio_currency: str | None
    blocks: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass
class NewsBatch:
    items: list[dict[str, Any]]
    retrieved_at: datetime
    run_id: str | None = None


class NewsProviderError(RuntimeError):
    """Displayable provider error: no credentials or raw HTTP bodies."""


class NewsProvider(Protocol):
    name: str
    is_fixture: bool

    def search(self, query: str, *, limit: int, lookback_days: int) -> NewsBatch: ...


@dataclass
class ArticleMatch:
    term: str
    field: str
    source_excerpt: str
    holdings: list[HoldingLink]
    evidence: list[SourceRef]
    exposures: list[ExposureLink] = field(default_factory=list)
    supporting_terms: list[str] = field(default_factory=list)


@dataclass
class NewsArticle:
    title: str
    url: str
    publisher: str
    snippet: str
    published_at: datetime
    published_at_raw: str
    date_is_estimated: bool
    retrieved_at: datetime
    relevance_score: float
    score_components: dict[str, float]
    matches: list[ArticleMatch]
    provider_run_ids: list[str]
    event_terms: list[str] = field(default_factory=list)
    impact_status: str = "not_assessed"
    relevance: dict[str, list] = field(default_factory=dict)


@dataclass
class NewsResult:
    client_ref: str
    portfolio_id: int
    as_of: datetime
    provider: str
    is_fixture: bool
    status: str
    queries: list[NewsQuery]
    portfolio_snapshot_at: str | None
    history_as_of: str | None
    articles: list[NewsArticle] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    deferred_queries: list[NewsQuery] = field(default_factory=list)
    rejected_counts: dict[str, int] = field(default_factory=dict)
    failed_queries: int = 0
    collection_stats: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "news-3.0"
