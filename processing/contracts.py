"""Shared contracts for the UnRiskOmega processing pipeline.

Agreed jointly by Mohamed (data foundation) and Khalil (analytics).
Mohamed maintains this file after agreement: changing a field here is an
interface change and needs both of us.

Stdlib only -- no runtime dependencies, so the integration team can import
this without installing anything.

Two rules this file exists to enforce:

1. "No data" is not one thing. A collection can be ABSENT (key not in the
   JSON), NULL (key present, value null), EMPTY (present, zero items) or
   INVALID (present, wrong type -- e.g. the "N/A" string produced by
   clients_cleaned.json). A health check that finds no violations because
   none were exported is NOT the same as one that found none because there
   are none. See `Presence`.

2. Every analytical number must be traceable back to a field in the source.
   See `SourceRef`, which doubles as the evidence object in the payload.

Normalization rules agreed with Khalil:

    input situation              what the analytics layer receives
    ---------------------------  ------------------------------------------------
    key absent                   [] + presence=ABSENT   -> .unavailable is True
    key present, value null      [] + presence=NULL     -> .unavailable is True
    explicitly empty list        [] + presence=EMPTY    -> .is_known_empty is True
    missing number               None, never 0
    missing identifier           None
    wrong type (e.g. "N/A")      [] + presence=INVALID + an ERROR Diagnostic

    A recorded collection is known empty only when .is_known_empty is True.
    Calculated checks may pass on complete, valid PRESENT data. On
    .unavailable they must emit a DataGap rather than an all-clear.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, is_dataclass
from dataclasses import fields as dc_fields
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, TypeVar

SCHEMA_VERSION = "1.1"


# --------------------------------------------------------------------------
# 1. Presence: absent vs null vs empty vs invalid
# --------------------------------------------------------------------------

class Presence(str, Enum):
    PRESENT = "present"   # key present, right type, has items
    EMPTY = "empty"       # key present, right type, zero items
    NULL = "null"         # key present, value was JSON null
    ABSENT = "absent"     # key not present in the source object
    INVALID = "invalid"   # key present, wrong type (string, number, ...)

    @property
    def has_items(self) -> bool:
        return self is Presence.PRESENT

    @property
    def is_confirmed_empty(self) -> bool:
        """True only when the source positively says 'there are none'.

        EMPTY means the exporter wrote []. NULL/ABSENT mean we were not told.
        Analyzers must not report 'no violations' on NULL or ABSENT.
        """
        return self is Presence.EMPTY


T = TypeVar("T")


@dataclass(frozen=True)
class SourceList(Generic[T]):
    """A list from the source, carrying why it might be empty."""

    items: list[T]
    presence: Presence
    path: str = ""            # e.g. "clients[12].SuitabilityViolations"
    raw_type: str | None = None   # type name when presence is INVALID

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def is_known_empty(self) -> bool:
        """The source positively told us there are none."""
        return self.presence.is_confirmed_empty

    @property
    def unavailable(self) -> bool:
        """We were NOT told. Never report a clean result on this."""
        return self.presence in (Presence.NULL, Presence.ABSENT, Presence.INVALID)

    @classmethod
    def absent(cls, path: str = "") -> SourceList[T]:
        return cls(items=[], presence=Presence.ABSENT, path=path)


# --------------------------------------------------------------------------
# 2. Evidence and diagnostics
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceRef:
    """Pointer back to the exact field a value came from.

    Serialized as-is into `evidence[]` in the briefing payload.
    """

    source: str = "clients.json"
    client_ref: str | None = None
    portfolio_id: int | None = None
    security_id: int | None = None
    account_name: str | None = None
    field: str | None = None
    value: Any = None
    path: str | None = None   # json path, for humans debugging a finding


class Level(str, Enum):
    ERROR = "error"       # record unusable or contradictory
    WARNING = "warning"   # usable, but something is off
    INFO = "info"         # worth noting, nothing wrong


@dataclass(frozen=True)
class Diagnostic:
    level: Level
    code: str             # stable machine code, e.g. "unresolved_portfolio"
    message: str
    source: SourceRef | None = None


# --------------------------------------------------------------------------
# 3. Money
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Money:
    """An amount never travels without its currency."""

    amount: float | None
    currency: str | None

    @property
    def is_known(self) -> bool:
        return self.amount is not None and self.currency is not None


# --------------------------------------------------------------------------
# 4. Source records (shapes verified against the supplied clients.json)
# --------------------------------------------------------------------------

@dataclass
class SecurityPosition:
    security_id: int | None
    isin: str | None
    valor: str | None                      # absent on 9/703
    security_name: str | None
    quantity: float | None
    price_per_unit: float | None
    currency: str | None
    total_amount_in_portfolio_currency: float | None
    portfolio_value_percentage: float | None   # source-reported weight, evidence only
    marginal_contribution_to_risk: float | None   # absent on 22/703
    contribution_volatility: float | None         # absent on 22/703
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountPosition:
    account_name: str | None
    currency: str | None                   # may be a crypto ticker -- NOT ordinary cash
    total_amount_in_portfolio_currency: float | None
    portfolio_value_percentage: float | None
    marginal_contribution_to_risk: float | None   # absent on 36/123
    contribution_volatility: float | None
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoryPoint:
    date: str | None       # kept as the source string; parsed value below
    parsed_date: date | None
    value: float | None
    source: SourceRef = field(default_factory=SourceRef)


@dataclass
class ViolationPathStep:
    field_name: str | None
    left_value: Any
    right_value: Any
    operator: int | None
    raw: dict[str, Any] = field(default_factory=dict)


class ViolationScope(str, Enum):
    PORTFOLIO = "portfolio"
    PORTFOLIO_NOT_IN_EXPORT = "portfolio_not_in_export"


@dataclass
class SuitabilityViolation:
    id: int | None
    rule_code: str | None
    rule_description: str | None
    error_level: int | None
    severity: str | None                   # "Error" | "Warning"
    portfolio_id: int | None
    violation_path: SourceList[ViolationPathStep]   # absent on 3/180
    last_violated_date_utc: str | None              # absent on 94/180
    security_isin: str | None                       # absent on 149/180
    scope: ViolationScope = ViolationScope.PORTFOLIO
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Proposal:
    proposal_id: int | None
    public_guid: str | None
    portfolio_id: int | None
    status_id: int | None
    status_name: str | None
    advisory_type_id: int | None
    advisory_type_name: str | None
    currency: str | None
    strategic_asset_allocation_id: int | None
    proposed_date_utc: str | None           # absent on 16/206
    finalized_date_utc: str | None          # absent on 81/206
    transactions_submitted_date_utc: str | None   # absent on 146/206
    reason: str | None
    notes: str | None
    expected_return: float | None
    volatility: float | None
    value_at_risk: float | None
    security_positions: SourceList[SecurityPosition]
    account_positions: SourceList[AccountPosition]
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transaction:
    transaction_id: int | None
    public_guid: str | None
    proposal_id: int | None
    security_id: int | None                 # absent on 101/1274
    isin: str | None
    security_name: str | None
    quantity_for_transaction: float | None
    currency: str | None
    total_amount: float | None
    is_expiry: bool | None
    forward_state: int | None               # absent on 614/1274
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClientNote:
    note: str | None
    created_by_date_utc: str | None
    parsed_date: date | None
    source: SourceRef = field(default_factory=SourceRef)


@dataclass
class Tag:
    tag_name: str | None
    tag_type_name: str | None
    scope: str | None
    source: SourceRef = field(default_factory=SourceRef)


@dataclass
class RuleOverride:
    rule_code: str | None
    rule_description: str | None
    source: SourceRef = field(default_factory=SourceRef)


@dataclass
class Portfolio:
    portfolio_id: int | None
    public_guid: str | None
    portfolio_nr: str | None
    name: str | None
    portfolio_currency: str | None
    reference_currency: str | None
    strategic_asset_allocation_id: int | None
    investment_service_id: int | None
    investment_service_name: str | None
    strategy_id: int | None
    strategy_name: str | None
    aum_in_default_currency: float | None
    liquidity_in_default_currency: float | None
    volatility: float | None                # absent on 7/57
    expected_return: float | None           # absent on 6/57
    value_at_risk: float | None             # absent on 6/57
    factory_date_utc: str | None            # absent on 2/57
    security_positions: SourceList[SecurityPosition]   # ABSENT on 8/57
    account_positions: SourceList[AccountPosition]
    performance_history: SourceList[HistoryPoint]
    client_ref: str | None = None
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Client:
    client_id: int | None
    client_ref: str | None
    first_name: str | None                  # null on 4/47 (companies)
    last_name: str | None
    company: str | None                     # set on 4/47
    is_client_a_company: bool | None
    is_employee: bool | None
    regulatory_client_type_id: int | None
    regulatory_client_type_name: str | None
    reporting_currency: str | None
    risk_profile_id: int | None             # null on 4/47
    risk_profile_name: str | None
    esg_profile_id: int | None              # null on 19/47
    esg_profile_name: str | None
    birthday: str | None
    profiling_date_utc: str | None
    aum_in_default_currency: float | None
    liquidity_in_default_currency: float | None
    portfolios: SourceList[Portfolio]
    proposals: SourceList[Proposal]              # NULL on 17/47
    transactions: SourceList[Transaction]        # NULL on 18/47
    suitability_violations: SourceList[SuitabilityViolation]   # NULL on 20/47
    rule_overrides: SourceList[RuleOverride]     # NULL on 44/47
    notes: SourceList[ClientNote]
    tags: SourceList[Tag]
    source: SourceRef = field(default_factory=SourceRef)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.company:
            return self.company
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or (self.client_ref or "unknown")


# --------------------------------------------------------------------------
# 5. Reference data (reference.json -- optional, may be unavailable)
# --------------------------------------------------------------------------

@dataclass
class ReferenceIndex:
    """reference.json, indexed. Optional: None means dependent checks are unavailable.

    Source counts: 504 securities, 54 suitability rules, 5 risk profiles,
    6 strategies, 16 SAAs, 48,101 fund look-through rows, 1 recommendation list.
    """

    # Securities[].Id -- NOTE: the key is "Id" here, "SecurityId" in clients.json
    securities: dict[int, dict[str, Any]] = field(default_factory=dict)
    # ISIN -> [security id, ...]. A list: repeated ISINs are kept, never deduped.
    securities_by_isin: dict[str, list[int]] = field(default_factory=dict)

    risk_profiles: dict[int, dict[str, Any]] = field(default_factory=dict)      # MaxVola, MaxPRC, EquityQuoteInPercent
    strategies: dict[int, dict[str, Any]] = field(default_factory=dict)         # VolatilityMinimum/Maximum
    investment_services: dict[int, dict[str, Any]] = field(default_factory=dict)
    esg_profiles: dict[int, dict[str, Any]] = field(default_factory=dict)

    strategic_allocations: dict[int, dict[str, Any]] = field(default_factory=dict)
    # (saa_id, dimension, category) -> {Min/Target/MaxPercentage}
    saa_bands: dict[tuple[int, str, str], dict[str, Any]] = field(default_factory=dict)

    # by RuleCode -> LIST: one code ("Overweight in the equity region ...") is
    # defined twice in reference.json, so a dict[str, dict] would silently drop one.
    rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lookthrough: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # FundSecurityId -> rows
    recommendation_lists: dict[int, dict[str, Any]] = field(default_factory=dict)
    recommended_security_ids: set[int] = field(default_factory=set)

    proposal_statuses: dict[int, dict[str, Any]] = field(default_factory=dict)
    advisory_types: dict[int, dict[str, Any]] = field(default_factory=dict)
    tags: dict[str, dict[str, Any]] = field(default_factory=dict)

    raw: dict[str, Any] = field(default_factory=dict)

    def security(self, security_id: int | None) -> dict[str, Any] | None:
        return self.securities.get(security_id) if security_id is not None else None

    def rule(self, rule_code: str | None) -> list[dict[str, Any]]:
        """All rule definitions for a code. Empty list means unresolved."""
        return self.rules.get(rule_code, []) if rule_code else []

    def band(self, saa_id: int | None, dimension: str, category: str) -> dict[str, Any] | None:
        if saa_id is None:
            return None
        return self.saa_bands.get((saa_id, dimension, category))


# --------------------------------------------------------------------------
# 6. Availability: a missing check is not a passed check
# --------------------------------------------------------------------------

class CheckStatus(str, Enum):
    AVAILABLE = "available"
    NO_REFERENCE_DATA = "unavailable_no_reference_data"
    NO_SOURCE_DATA = "unavailable_no_source_data"
    NOT_IMPLEMENTED = "unavailable_not_implemented"


@dataclass(frozen=True)
class Availability:
    status: CheckStatus
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.AVAILABLE


# Keys used in ClientDossier.availability. Agreed with Khalil.
CHECK_SUITABILITY = "suitability"
CHECK_ALLOCATION = "allocation"
CHECK_CONCENTRATION = "concentration"
CHECK_DEVELOPMENT = "development"
CHECK_RISK_LIMITS = "risk_limits"
CHECK_LIQUIDITY = "liquidity"
CHECK_PROPOSALS = "proposals"
CHECK_LOOKTHROUGH = "lookthrough"
CHECK_CLIENT_CONTEXT = "client_context"


# --------------------------------------------------------------------------
# 7. DataStore and ClientDossier -- Mohamed's deliverables
# --------------------------------------------------------------------------

@dataclass
class DataStore:
    """Everything loaded, indexed, nothing calculated."""

    clients: dict[str, Client] = field(default_factory=dict)          # by ClientRef
    portfolios: dict[int, Portfolio] = field(default_factory=dict)    # by PortfolioId
    portfolio_owner: dict[int, str] = field(default_factory=dict)     # PortfolioId -> ClientRef
    reference: ReferenceIndex | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    loaded_at: datetime | None = None

    @property
    def has_reference(self) -> bool:
        return self.reference is not None

    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level is Level.ERROR]


@dataclass
class ClientDossier:
    """One client + one portfolio, fully resolved. Khalil's only input."""

    client_ref: str
    portfolio_id: int
    analysis_date: date
    client: Client
    portfolio: Portfolio

    security_positions: SourceList[SecurityPosition]
    account_positions: SourceList[AccountPosition]
    performance_history: SourceList[HistoryPoint]

    violations: SourceList[SuitabilityViolation]            # scoped to this portfolio
    unscoped_violations: list[SuitabilityViolation] = field(default_factory=list)
    proposals: SourceList[Proposal] = field(default_factory=lambda: SourceList.absent())
    transactions: SourceList[Transaction] = field(default_factory=lambda: SourceList.absent())
    notes: SourceList[ClientNote] = field(default_factory=lambda: SourceList.absent())
    tags: SourceList[Tag] = field(default_factory=lambda: SourceList.absent())
    rule_overrides: SourceList[RuleOverride] = field(default_factory=lambda: SourceList.absent())

    risk_profile: dict[str, Any] | None = None          # from reference.json
    strategic_allocation: dict[str, Any] | None = None  # from reference.json
    securities: dict[int, dict[str, Any]] = field(default_factory=dict)

    is_consolidated: bool | None = None
    sibling_portfolio_ids: list[int] = field(default_factory=list)

    # Latest observation in the data, NOT today. The supplied export ends
    # 2026-07-01, so recency must be measured against this, not the clock.
    data_as_of: date | None = None
    data_age_days: int | None = None

    availability: dict[str, Availability] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def check(self, name: str) -> Availability:
        return self.availability.get(
            name, Availability(CheckStatus.NOT_IMPLEMENTED, f"{name} not evaluated")
        )


# --------------------------------------------------------------------------
# 8. Briefing payload -- Khalil's output, shape agreed jointly
# --------------------------------------------------------------------------

class Section(int, Enum):
    DEVELOPMENT = 1      # Recent Portfolio Development
    HEALTH = 2           # Portfolio Health Check
    OUTLOOK = 3          # Outlook and Next Best Actions


@dataclass
class Finding:
    id: str                       # "CASE-016:315:concentration:18477"
    section: Section
    kind: str                     # "risk" | "suitability" | "allocation" | ...
    headline: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[SourceRef] = field(default_factory=list)
    selection_reason: str | None = None
    score: float | None = None
    score_components: dict[str, float] = field(default_factory=dict)
    analyzer: str | None = None
    calculation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("A finding must have at least one evidence reference")


@dataclass
class ActionCandidate:
    verb: str                     # "review" | "confirm" | "resolve" | "follow_up"
    target: str
    finding_ids: list[str] = field(default_factory=list)
    indicative: bool = True
    assumptions: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    rationale: str | None = None


@dataclass
class DataGap:
    check: str
    status: CheckStatus
    reason: str


@dataclass
class BriefingPayload:
    client_ref: str
    portfolio_id: int
    client_context: dict[str, Any]
    selected_findings: list[Finding] = field(default_factory=list)
    action_candidates: list[ActionCandidate] = field(default_factory=list)
    data_gaps: list[DataGap] = field(default_factory=list)
    critical_overflow_count: int = 0
    analysis_date: date | None = None
    schema_version: str = SCHEMA_VERSION
    is_fixture: bool = False      # True => example data, not a real analysis
    # Optional section-3 external context (news matched to this portfolio's
    # holdings). None means news was not attached; the core runs identically
    # without it. Populated by news_integration.attach_news().
    news: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# 9. Serialization: unavailable is JSON null, never NaN and never "N/A"
# --------------------------------------------------------------------------

def to_jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, SourceList):
        return {
            "items": [to_jsonable(i) for i in obj.items],
            "presence": obj.presence.value,
            "path": obj.path,
            "raw_type": obj.raw_type,
        }
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: to_jsonable(getattr(obj, f.name))
            for f in dc_fields(obj)
            if f.name != "raw"
        }
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(i) for i in obj]
    return str(obj)
