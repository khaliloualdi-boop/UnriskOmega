"""M2/M5 -- clients.json -> DataStore.

Owner: Mohamed.

Normalization rules agreed with Khalil:

    key absent               -> [] + presence=ABSENT   (.unavailable)
    key present, value null  -> [] + presence=NULL     (.unavailable)
    explicitly empty list    -> [] + presence=EMPTY    (.is_known_empty)
    missing number           -> None, never 0
    missing identifier       -> None
    wrong type, e.g. "N/A"   -> [] + presence=INVALID + an ERROR diagnostic

Loading never raises on bad records: a broken client produces diagnostics and is
skipped, the other 46 still load. Only an unusable *input shape* raises
InputValidationError, so the integration team gets one clear message.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from .contracts import (
    AccountPosition,
    Client,
    ClientNote,
    DataStore,
    Diagnostic,
    HistoryPoint,
    Level,
    Portfolio,
    Presence,
    Proposal,
    RuleOverride,
    SecurityPosition,
    SourceList,
    SourceRef,
    SuitabilityViolation,
    Tag,
    Transaction,
    ViolationPathStep,
)
from .indexes import build_reference_index

_MISSING = object()

# Strings the exporter (or clients_cleaned.json) uses where a value is absent.
_PLACEHOLDERS = {"", "N/A", "NA", "N.A.", "NULL", "NONE", "-", "--"}

DUPLICATE_ERROR = "error"          # refuse the load, list the duplicates
DUPLICATE_REPLACE = "replace"      # later record wins, loudly
DUPLICATE_KEEP_FIRST = "keep_first"
_DUPLICATE_POLICIES = (DUPLICATE_ERROR, DUPLICATE_REPLACE, DUPLICATE_KEEP_FIRST)


class InputValidationError(ValueError):
    """The supplied data is not a usable clients export."""

    def __init__(self, message: str, diagnostics: list[Diagnostic] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------

_FRACTION = re.compile(r"\.(\d+)")


def parse_timestamp(value: Any) -> date | None:
    """Parse the five date shapes present in clients.json.

        2021-10-01                          (history)
        1976-09-19                          (birthday)
        2026-09-05T09:16:32                 (notes)
        2023-11-06T23:00:00+01:00           (profiling)
        2026-08-15T09:53:29.943Z            (violations, 2-3 frac digits)
        2023-11-11T16:05:42.3970767Z        (.NET ticks, 7 frac digits)

    fromisoformat accepts exactly 3 or 6 fractional digits on older Pythons, so
    the fraction is normalized to 6 before parsing. Returns None rather than
    raising: an unparseable date is a diagnostic, not a crash.
    """
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.upper() in _PLACEHOLDERS:
        return None

    match = _FRACTION.search(text)
    if match:
        digits = match.group(1)[:6].ljust(6, "0")
        text = f"{text[:match.start()]}.{digits}{text[match.end():]}"
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 -- source calendar date only
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Reader: every scalar and collection goes through here
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, source: str) -> None:
        self.source = source
        self.diagnostics: list[Diagnostic] = []
        self._saw_placeholder_collection = False

    # -- diagnostics --------------------------------------------------------

    def note(self, level: Level, code: str, message: str, path: str | None = None,
             **ctx: Any) -> None:
        self.diagnostics.append(Diagnostic(level, code, message, self.ref(path, **ctx)))

    def ref(self, path: str | None = None, *, client_ref: str | None = None,
            portfolio_id: int | None = None, security_id: int | None = None,
            account_name: str | None = None, field: str | None = None,
            value: Any = None) -> SourceRef:
        return SourceRef(
            source=self.source, client_ref=client_ref, portfolio_id=portfolio_id,
            security_id=security_id, account_name=account_name, field=field,
            value=value, path=path,
        )

    # -- scalars ------------------------------------------------------------

    def number(self, obj: dict[str, Any], key: str, path: str, **ctx: Any) -> float | None:
        raw = obj.get(key, _MISSING)
        if raw is _MISSING or raw is None:
            return None
        if isinstance(raw, bool):
            self.note(Level.WARNING, "bool_where_number",
                      f"{path}.{key} is a boolean, expected a number", path, field=key,
                      value=raw, **ctx)
            return None
        if isinstance(raw, (int, float)):
            if math.isnan(raw) or math.isinf(raw):
                self.note(Level.WARNING, "non_finite_number",
                          f"{path}.{key} is {raw}", path, field=key, **ctx)
                return None
            return float(raw)
        if isinstance(raw, str):
            text = raw.strip()
            if text.upper() in _PLACEHOLDERS:
                self.note(Level.ERROR, "placeholder_where_number",
                          f"{path}.{key} is {raw!r}; a placeholder is not a value",
                          path, field=key, value=raw, **ctx)
                return None
            try:
                number = float(text.replace("'", "").replace(" ", ""))
            except ValueError:
                pass
            else:
                if math.isfinite(number):
                    return number
                self.note(Level.ERROR, "non_finite_number",
                          f"{path}.{key} is not a finite number", path,
                          field=key, value=raw, **ctx)
                return None
        self.note(Level.ERROR, "invalid_number",
                  f"{path}.{key} is {type(raw).__name__} {raw!r}, expected a number",
                  path, field=key, value=raw, **ctx)
        return None

    def integer(self, obj: dict[str, Any], key: str, path: str, **ctx: Any) -> int | None:
        raw = obj.get(key, _MISSING)
        if raw is _MISSING or raw is None:
            return None
        if isinstance(raw, bool):
            self.note(Level.WARNING, "bool_where_integer",
                      f"{path}.{key} is a boolean, expected an integer", path,
                      field=key, value=raw, **ctx)
            return None
        if isinstance(raw, int):
            return raw
        value = self.number(obj, key, path, **ctx)
        if value is None:
            return None
        if value.is_integer():
            return int(value)
        self.note(Level.WARNING, "non_integer_identifier",
                  f"{path}.{key} is {value}, expected a whole number", path,
                  field=key, value=raw, **ctx)
        return None

    def text(self, obj: dict[str, Any], key: str, path: str, **ctx: Any) -> str | None:
        raw = obj.get(key, _MISSING)
        if raw is _MISSING or raw is None:
            return None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            if stripped.upper() in _PLACEHOLDERS:
                self.note(Level.WARNING, "placeholder_where_text",
                          f"{path}.{key} is {raw!r}; treated as absent", path,
                          field=key, value=raw, **ctx)
                return None
            return raw          # original spelling, trailing spaces and all
        if isinstance(raw, (int, float, bool)):
            return str(raw)
        self.note(Level.WARNING, "invalid_text",
                  f"{path}.{key} is {type(raw).__name__}, expected a string", path,
                  field=key, **ctx)
        return None

    def flag(self, obj: dict[str, Any], key: str, path: str, **ctx: Any) -> bool | None:
        raw = obj.get(key, _MISSING)
        if raw is _MISSING or raw is None:
            return None
        if isinstance(raw, bool):
            return raw
        self.note(Level.WARNING, "invalid_boolean",
                  f"{path}.{key} is {type(raw).__name__}, expected true/false", path,
                  field=key, value=raw, **ctx)
        return None

    # -- collections --------------------------------------------------------

    def collection(self, obj: dict[str, Any], key: str, path: str,
                   parse_item: Callable[[dict[str, Any], str], Any],
                   **ctx: Any) -> SourceList:
        field_path = f"{path}.{key}"
        raw = obj.get(key, _MISSING)

        if raw is _MISSING:
            return SourceList([], Presence.ABSENT, field_path)
        if raw is None:
            return SourceList([], Presence.NULL, field_path)

        if isinstance(raw, list):
            items: list[Any] = []
            invalid_entries = False
            for i, entry in enumerate(raw):
                item_path = f"{field_path}[{i}]"
                if not isinstance(entry, dict):
                    invalid_entries = True
                    self.note(Level.ERROR, "invalid_list_item",
                              f"{item_path} is {type(entry).__name__}, expected an object",
                              item_path, **ctx)
                    continue
                items.append(parse_item(entry, item_path))
            if invalid_entries:
                # Retain usable rows for inspection, but the collection is
                # incomplete and must not support a clean analytical result.
                presence = Presence.INVALID
            elif not raw:
                presence = Presence.EMPTY
            elif items:
                presence = Presence.PRESENT
            else:
                # had entries, none usable -- not a confirmed empty collection
                presence = Presence.INVALID
            return SourceList(items, presence, field_path)

        hint = ""
        if isinstance(raw, str) and raw.strip().upper() in _PLACEHOLDERS:
            hint = (" -- this looks like clients_cleaned.json, where nulls were "
                    "replaced by \"N/A\"; load clients.json instead")
            self._saw_placeholder_collection = True
        self.note(Level.ERROR, "invalid_collection_type",
                  f"{field_path} is {type(raw).__name__} {raw!r}, expected a list{hint}",
                  field_path, **ctx)
        return SourceList([], Presence.INVALID, field_path, raw_type=type(raw).__name__)


# --------------------------------------------------------------------------
# Record parsers
# --------------------------------------------------------------------------

def _security_position(r: _Reader, obj: dict[str, Any], path: str,
                       client_ref: str | None, portfolio_id: int | None) -> SecurityPosition:
    ctx = {"client_ref": client_ref, "portfolio_id": portfolio_id}
    security_id = r.integer(obj, "SecurityId", path, **ctx)
    return SecurityPosition(
        security_id=security_id,
        isin=r.text(obj, "Isin", path, **ctx),
        valor=r.text(obj, "Valor", path, **ctx),
        security_name=r.text(obj, "SecurityName", path, **ctx),
        quantity=r.number(obj, "Quantity", path, **ctx),
        price_per_unit=r.number(obj, "PricePerUnit", path, **ctx),
        currency=r.text(obj, "Currency", path, **ctx),
        total_amount_in_portfolio_currency=r.number(
            obj, "TotalAmountInPortfolioCurrency", path, **ctx),
        portfolio_value_percentage=r.number(obj, "PortfolioValuePercentage", path, **ctx),
        marginal_contribution_to_risk=r.number(obj, "MarginalContributionToRisk", path, **ctx),
        contribution_volatility=r.number(obj, "ContributionVolatility", path, **ctx),
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id,
                     security_id=security_id),
        raw=obj,
    )


def _account_position(r: _Reader, obj: dict[str, Any], path: str,
                      client_ref: str | None, portfolio_id: int | None) -> AccountPosition:
    ctx = {"client_ref": client_ref, "portfolio_id": portfolio_id}
    name = r.text(obj, "AccountName", path, **ctx)
    return AccountPosition(
        account_name=name,
        currency=r.text(obj, "Currency", path, **ctx),
        total_amount_in_portfolio_currency=r.number(
            obj, "TotalAmountInPortfolioCurrency", path, **ctx),
        portfolio_value_percentage=r.number(obj, "PortfolioValuePercentage", path, **ctx),
        marginal_contribution_to_risk=r.number(obj, "MarginalContributionToRisk", path, **ctx),
        contribution_volatility=r.number(obj, "ContributionVolatility", path, **ctx),
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id,
                     account_name=name),
        raw=obj,
    )


def _history_point(r: _Reader, obj: dict[str, Any], path: str,
                   client_ref: str | None, portfolio_id: int | None) -> HistoryPoint:
    ctx = {"client_ref": client_ref, "portfolio_id": portfolio_id}
    raw_date = r.text(obj, "Date", path, **ctx)
    parsed = parse_timestamp(raw_date)
    if raw_date and parsed is None:
        r.note(Level.WARNING, "unparseable_date",
               f"{path}.Date is {raw_date!r}", path, field="Date", **ctx)
    return HistoryPoint(
        date=raw_date,
        parsed_date=parsed,
        value=r.number(obj, "Value", path, **ctx),
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id),
    )


def _violation_step(r: _Reader, obj: dict[str, Any], path: str) -> ViolationPathStep:
    return ViolationPathStep(
        field_name=r.text(obj, "FieldName", path),
        left_value=obj.get("LeftValue"),
        right_value=obj.get("RightValue"),
        operator=r.integer(obj, "Operator", path),
        raw=obj,
    )


def _violation(r: _Reader, obj: dict[str, Any], path: str,
               client_ref: str | None) -> SuitabilityViolation:
    ctx = {"client_ref": client_ref}
    portfolio_id = r.integer(obj, "PortfolioId", path, **ctx)
    return SuitabilityViolation(
        id=r.integer(obj, "Id", path, **ctx),
        rule_code=r.text(obj, "RuleCode", path, **ctx),
        rule_description=r.text(obj, "RuleDescription", path, **ctx),
        error_level=r.integer(obj, "ErrorLevel", path, **ctx),
        severity=r.text(obj, "Severity", path, **ctx),
        portfolio_id=portfolio_id,
        violation_path=r.collection(obj, "ViolationPath", path,
                                    lambda o, p: _violation_step(r, o, p), **ctx),
        last_violated_date_utc=r.text(obj, "LastViolatedDateUTC", path, **ctx),
        security_isin=r.text(obj, "SecurityIsin", path, **ctx),
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id),
        raw=obj,
    )


def _proposal(r: _Reader, obj: dict[str, Any], path: str,
              client_ref: str | None) -> Proposal:
    ctx = {"client_ref": client_ref}
    portfolio_id = r.integer(obj, "PortfolioId", path, **ctx)
    pos_ctx = {"client_ref": client_ref, "portfolio_id": portfolio_id}
    return Proposal(
        proposal_id=r.integer(obj, "ProposalId", path, **ctx),
        public_guid=r.text(obj, "PublicGuid", path, **ctx),
        portfolio_id=portfolio_id,
        status_id=r.integer(obj, "ProposalStatusId", path, **ctx),
        status_name=r.text(obj, "ProposalStatusName", path, **ctx),
        advisory_type_id=r.integer(obj, "AdvisoryTypeId", path, **ctx),
        advisory_type_name=r.text(obj, "AdvisoryTypeName", path, **ctx),
        currency=r.text(obj, "Currency", path, **ctx),
        strategic_asset_allocation_id=r.integer(obj, "StrategicAssetAllocationId", path, **ctx),
        proposed_date_utc=r.text(obj, "ProposedDateUTC", path, **ctx),
        finalized_date_utc=r.text(obj, "FinalizedDateUTC", path, **ctx),
        transactions_submitted_date_utc=r.text(obj, "TransactionsSubmittedDateUTC", path, **ctx),
        reason=r.text(obj, "Reason", path, **ctx),
        notes=r.text(obj, "Notes", path, **ctx),
        expected_return=r.number(obj, "ExpectedReturn", path, **ctx),
        volatility=r.number(obj, "Volatility", path, **ctx),
        value_at_risk=r.number(obj, "ValueAtRisk", path, **ctx),
        security_positions=r.collection(
            obj, "SecurityPositions", path,
            lambda o, p: _security_position(r, o, p, client_ref, portfolio_id), **pos_ctx),
        account_positions=r.collection(
            obj, "AccountPositions", path,
            lambda o, p: _account_position(r, o, p, client_ref, portfolio_id), **pos_ctx),
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id),
        raw=obj,
    )


def _transaction(r: _Reader, obj: dict[str, Any], path: str,
                 client_ref: str | None) -> Transaction:
    ctx = {"client_ref": client_ref}
    security_id = r.integer(obj, "SecurityId", path, **ctx)
    return Transaction(
        transaction_id=r.integer(obj, "TransactionId", path, **ctx),
        public_guid=r.text(obj, "PublicGuid", path, **ctx),
        proposal_id=r.integer(obj, "ProposalId", path, **ctx),
        security_id=security_id,
        isin=r.text(obj, "Isin", path, **ctx),
        security_name=r.text(obj, "SecurityName", path, **ctx),
        quantity_for_transaction=r.number(obj, "QuantityForTransaction", path, **ctx),
        currency=r.text(obj, "Currency", path, **ctx),
        total_amount=r.number(obj, "TotalAmount", path, **ctx),
        is_expiry=r.flag(obj, "IsExpiry", path, **ctx),
        forward_state=r.integer(obj, "ForwardState", path, **ctx),
        source=r.ref(path, client_ref=client_ref, security_id=security_id),
        raw=obj,
    )


def _note(r: _Reader, obj: dict[str, Any], path: str, client_ref: str | None) -> ClientNote:
    created = r.text(obj, "CreatedByDateUTC", path, client_ref=client_ref)
    return ClientNote(
        note=r.text(obj, "Note", path, client_ref=client_ref),
        created_by_date_utc=created,
        parsed_date=parse_timestamp(created),
        source=r.ref(path, client_ref=client_ref),
    )


def _tag(r: _Reader, obj: dict[str, Any], path: str, client_ref: str | None) -> Tag:
    return Tag(
        tag_name=r.text(obj, "TagName", path, client_ref=client_ref),
        tag_type_name=r.text(obj, "TagTypeName", path, client_ref=client_ref),
        scope=r.text(obj, "Scope", path, client_ref=client_ref),
        source=r.ref(path, client_ref=client_ref),
    )


def _override(r: _Reader, obj: dict[str, Any], path: str,
              client_ref: str | None) -> RuleOverride:
    return RuleOverride(
        rule_code=r.text(obj, "RuleCode", path, client_ref=client_ref),
        rule_description=r.text(obj, "RuleDescription", path, client_ref=client_ref),
        source=r.ref(path, client_ref=client_ref),
    )


def _portfolio(r: _Reader, obj: dict[str, Any], path: str,
               client_ref: str | None) -> Portfolio:
    ctx = {"client_ref": client_ref}
    portfolio_id = r.integer(obj, "PortfolioId", path, **ctx)
    pos_ctx = {"client_ref": client_ref, "portfolio_id": portfolio_id}
    return Portfolio(
        portfolio_id=portfolio_id,
        public_guid=r.text(obj, "PublicGuid", path, **ctx),
        portfolio_nr=r.text(obj, "PortfolioNr", path, **ctx),
        name=r.text(obj, "Name", path, **ctx),
        portfolio_currency=r.text(obj, "PortfolioCurrency", path, **ctx),
        reference_currency=r.text(obj, "ReferenceCurrency", path, **ctx),
        strategic_asset_allocation_id=r.integer(obj, "StrategicAssetAllocationId", path, **ctx),
        investment_service_id=r.integer(obj, "InvestmentServiceId", path, **ctx),
        investment_service_name=r.text(obj, "InvestmentServiceName", path, **ctx),
        strategy_id=r.integer(obj, "StrategyId", path, **ctx),
        strategy_name=r.text(obj, "StrategyName", path, **ctx),
        aum_in_default_currency=r.number(
            obj, "AssetsUnderManagementInDefaultCurrency", path, **ctx),
        liquidity_in_default_currency=r.number(
            obj, "LiquidityInDefaultCurrency", path, **ctx),
        volatility=r.number(obj, "Volatility", path, **ctx),
        expected_return=r.number(obj, "ExpectedReturn", path, **ctx),
        value_at_risk=r.number(obj, "ValueAtRisk", path, **ctx),
        factory_date_utc=r.text(obj, "FactoryDateUtc", path, **ctx),
        security_positions=r.collection(
            obj, "SecurityPositions", path,
            lambda o, p: _security_position(r, o, p, client_ref, portfolio_id), **pos_ctx),
        account_positions=r.collection(
            obj, "AccountPositions", path,
            lambda o, p: _account_position(r, o, p, client_ref, portfolio_id), **pos_ctx),
        performance_history=r.collection(
            obj, "PerformanceHistory", path,
            lambda o, p: _history_point(r, o, p, client_ref, portfolio_id), **pos_ctx),
        client_ref=client_ref,
        source=r.ref(path, client_ref=client_ref, portfolio_id=portfolio_id),
        raw=obj,
    )


def _client(r: _Reader, obj: dict[str, Any], path: str) -> Client:
    client_ref = r.text(obj, "ClientRef", path)
    ctx = {"client_ref": client_ref}
    return Client(
        client_id=r.integer(obj, "ClientId", path, **ctx),
        client_ref=client_ref,
        first_name=r.text(obj, "FirstName", path, **ctx),
        last_name=r.text(obj, "LastName", path, **ctx),
        company=r.text(obj, "Company", path, **ctx),
        is_client_a_company=r.flag(obj, "IsClientACompany", path, **ctx),
        is_employee=r.flag(obj, "IsEmployee", path, **ctx),
        regulatory_client_type_id=r.integer(obj, "RegulatoryClientTypeId", path, **ctx),
        regulatory_client_type_name=r.text(obj, "RegulatoryClientTypeName", path, **ctx),
        reporting_currency=r.text(obj, "ReportingCurrency", path, **ctx),
        risk_profile_id=r.integer(obj, "RiskProfileId", path, **ctx),
        risk_profile_name=r.text(obj, "RiskProfileName", path, **ctx),
        esg_profile_id=r.integer(obj, "EsgProfileId", path, **ctx),
        esg_profile_name=r.text(obj, "EsgProfileName", path, **ctx),
        birthday=r.text(obj, "Birthday", path, **ctx),
        profiling_date_utc=r.text(obj, "ProfilingDateUtc", path, **ctx),
        aum_in_default_currency=r.number(
            obj, "AssetsUnderManagementInDefaultCurrency", path, **ctx),
        liquidity_in_default_currency=r.number(
            obj, "LiquidityInDefaultCurrency", path, **ctx),
        portfolios=r.collection(obj, "Portfolios", path,
                                lambda o, p: _portfolio(r, o, p, client_ref), **ctx),
        proposals=r.collection(obj, "Proposals", path,
                               lambda o, p: _proposal(r, o, p, client_ref), **ctx),
        transactions=r.collection(obj, "Transactions", path,
                                  lambda o, p: _transaction(r, o, p, client_ref), **ctx),
        suitability_violations=r.collection(obj, "SuitabilityViolations", path,
                                            lambda o, p: _violation(r, o, p, client_ref), **ctx),
        rule_overrides=r.collection(obj, "IndividualRuleOverrides", path,
                                    lambda o, p: _override(r, o, p, client_ref), **ctx),
        notes=r.collection(obj, "ClientNotes", path,
                           lambda o, p: _note(r, o, p, client_ref), **ctx),
        tags=r.collection(obj, "Tags", path,
                          lambda o, p: _tag(r, o, p, client_ref), **ctx),
        source=r.ref(path, client_ref=client_ref),
        raw=obj,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def load_store(clients_data: Any, reference_data: Any = None, *,
               source: str = "clients.json",
               on_duplicate: str = DUPLICATE_ERROR) -> DataStore:
    """Parse and index a clients export. The integration team's entry point.

    `clients_data` is already-parsed JSON: a list of client objects, or an
    object wrapping one under "Clients". Never a filename -- the integration
    team owns the upload path (M5).
    """
    if on_duplicate not in _DUPLICATE_POLICIES:
        raise ValueError(
            f"on_duplicate must be one of {_DUPLICATE_POLICIES}, got {on_duplicate!r}")

    reader = _Reader(source)
    entries = _as_client_list(clients_data, reader)

    store = DataStore(loaded_at=datetime.now(UTC))
    duplicates: list[str] = []

    for i, entry in enumerate(entries):
        # Key the evidence path by ClientRef, not batch position: the same client
        # must produce the same paths whether it arrives in a file of 47 or of 7.
        path = f"clients[{i}]"
        if not isinstance(entry, dict):
            reader.note(Level.ERROR, "invalid_client_record",
                        f"{path} is {type(entry).__name__}, expected an object", path)
            continue
        declared_ref = entry.get("ClientRef")
        if isinstance(declared_ref, str) and declared_ref.strip():
            path = f"clients[{declared_ref.strip()}]"

        client = _client(reader, entry, path)
        ref = client.client_ref
        if not ref:
            reader.note(Level.ERROR, "client_without_ref",
                        f"{path} has no usable ClientRef; record skipped", path)
            continue

        if ref in store.clients:
            duplicates.append(ref)
            if on_duplicate == DUPLICATE_KEEP_FIRST:
                reader.note(Level.ERROR, "duplicate_client",
                            f"ClientRef {ref} appears more than once; keeping the first",
                            path, client_ref=ref)
                continue
            reader.note(Level.ERROR, "duplicate_client",
                        f"ClientRef {ref} appears more than once; "
                        f"{'replacing the earlier record' if on_duplicate == DUPLICATE_REPLACE else 'refusing the load'}",
                        path, client_ref=ref)
            if on_duplicate == DUPLICATE_ERROR:
                continue
            _forget_client(store, ref)

        store.clients[ref] = client
        for portfolio in client.portfolios:
            pid = portfolio.portfolio_id
            if pid is None:
                reader.note(Level.ERROR, "portfolio_without_id",
                            f"a portfolio of {ref} has no PortfolioId; not indexed",
                            portfolio.source.path, client_ref=ref)
                continue
            if pid in store.portfolios:
                reader.note(Level.ERROR, "duplicate_portfolio_id",
                            f"PortfolioId {pid} is claimed by {store.portfolio_owner[pid]} "
                            f"and {ref}; keeping the first",
                            portfolio.source.path, client_ref=ref, portfolio_id=pid)
                continue
            store.portfolios[pid] = portfolio
            store.portfolio_owner[pid] = ref

    if duplicates and on_duplicate == DUPLICATE_ERROR:
        store.diagnostics.extend(reader.diagnostics)
        raise InputValidationError(
            f"duplicate ClientRef(s): {', '.join(sorted(set(duplicates)))}. "
            f"Pass on_duplicate='replace' to let later records win, or "
            f"'keep_first' to ignore them.",
            store.diagnostics,
        )

    if reader._saw_placeholder_collection:
        reader.note(Level.ERROR, "placeholder_source_suspected",
                    "collections containing placeholder strings were found: this input "
                    "looks like clients_cleaned.json. Load clients.json instead.")

    if reference_data is not None:
        store.reference = build_reference_index(reference_data, reader.diagnostics)

    store.diagnostics.extend(reader.diagnostics)
    return store


def _as_client_list(clients_data: Any, reader: _Reader) -> list[Any]:
    if isinstance(clients_data, list):
        return clients_data
    if isinstance(clients_data, dict):
        for key in ("Clients", "clients", "data", "Data"):
            value = clients_data.get(key)
            if isinstance(value, list):
                return value
        raise InputValidationError(
            "clients_data is an object with no client list under "
            "'Clients'/'clients'/'data'. Pass the parsed list of clients.")
    raise InputValidationError(
        f"clients_data must be a list of client objects (or an object wrapping one), "
        f"got {type(clients_data).__name__}")


def _forget_client(store: DataStore, client_ref: str) -> None:
    store.clients.pop(client_ref, None)
    stale = [pid for pid, owner in store.portfolio_owner.items() if owner == client_ref]
    for pid in stale:
        store.portfolio_owner.pop(pid, None)
        store.portfolios.pop(pid, None)
