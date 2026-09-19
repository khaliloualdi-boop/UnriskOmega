"""M4 -- DataStore -> ClientDossier.

Owner: Mohamed. This is the function Khalil calls.

    build_dossier(store, "CASE-016", 315) -> ClientDossier

Rules enforced here:

  * A violation is scoped to the portfolio it names. Violations naming a
    portfolio absent from the export (27 of 180) are kept at client level in
    `unscoped_violations`, never silently attached to this portfolio.
  * A consolidated portfolio (InvestmentServiceName == "Consolidated") is never
    merged with its components. It is flagged; its siblings are listed.
  * Every check gets an Availability. "not checked" must never look like "passed".
  * `data_as_of` is the latest observation in the data, not today's date.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any

from .contracts import (
    CHECK_ALLOCATION,
    CHECK_CLIENT_CONTEXT,
    CHECK_CONCENTRATION,
    CHECK_DEVELOPMENT,
    CHECK_LIQUIDITY,
    CHECK_LOOKTHROUGH,
    CHECK_PROPOSALS,
    CHECK_RISK_LIMITS,
    CHECK_SUITABILITY,
    Availability,
    CheckStatus,
    Client,
    ClientDossier,
    DataStore,
    Diagnostic,
    Level,
    Portfolio,
    Presence,
    SourceList,
    SourceRef,
    SuitabilityViolation,
    ViolationScope,
)
from .indexes import band_has_limits

CONSOLIDATED_SERVICE = "Consolidated"


class DossierError(ValueError):
    """The requested client/portfolio cannot produce a dossier."""


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def is_consolidated(portfolio: Portfolio) -> bool:
    return (portfolio.investment_service_name or "").strip() == CONSOLIDATED_SERVICE


def select_portfolio(store: DataStore, client_ref: str) -> int:
    """Deterministic policy when the caller does not name a portfolio.

    Largest AUM, excluding consolidated portfolios, ties broken by the lowest
    PortfolioId. Documented rather than clever: the integration team can pass an
    explicit portfolio_id whenever it knows better.
    """
    client = _require_client(store, client_ref)
    candidates = [p for p in client.portfolios if p.portfolio_id is not None]
    if not candidates:
        raise DossierError(f"{client_ref} has no portfolio with a PortfolioId")
    ordinary = [p for p in candidates if not is_consolidated(p)] or candidates
    ordinary.sort(key=lambda p: (-(p.aum_in_default_currency or 0.0), p.portfolio_id))
    return ordinary[0].portfolio_id


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_dossier(store: DataStore, client_ref: str,
                  portfolio_id: int | None = None, *,
                  analysis_date: date | None = None) -> ClientDossier:
    client = _require_client(store, client_ref)
    if portfolio_id is None:
        portfolio_id = select_portfolio(store, client_ref)

    portfolio = next(
        (p for p in client.portfolios if p.portfolio_id == portfolio_id), None)
    if portfolio is None:
        owner = store.portfolio_owner.get(portfolio_id)
        if owner:
            raise DossierError(
                f"portfolio {portfolio_id} belongs to {owner}, not {client_ref}")
        raise DossierError(f"portfolio {portfolio_id} is not in the export")

    # Keep loader errors visible to the analytics and API consumer. Include
    # client-level records and this portfolio, but not sibling diagnostics.
    prefix = portfolio.source.path or ""
    notes: list[Diagnostic] = []
    for diagnostic in store.diagnostics:
        source = diagnostic.source
        if source is None or source.client_ref is None:
            notes.append(diagnostic)
        elif source.client_ref == client_ref:
            path = source.path or ""
            if source.portfolio_id == portfolio_id or (
                source.portfolio_id is None
                and (".Portfolios[" not in path or path == prefix
                     or path.startswith(prefix + "."))
            ):
                notes.append(diagnostic)

    def note(level: Level, code: str, message: str) -> None:
        notes.append(Diagnostic(level, code, message,
                                SourceRef(client_ref=client_ref,
                                          portfolio_id=portfolio_id)))

    siblings = [p.portfolio_id for p in client.portfolios
                if p.portfolio_id is not None and p.portfolio_id != portfolio_id]
    consolidated = is_consolidated(portfolio)
    if consolidated:
        note(Level.WARNING, "consolidated_portfolio",
             f"portfolio {portfolio_id} is a consolidated view; its holdings overlap "
             f"portfolios {siblings or 'not present in this export'}. Do not add it to "
             f"its components.")
    elif any(is_consolidated(p) for p in client.portfolios):
        note(Level.INFO, "client_has_consolidated_portfolio",
             f"{client_ref} also has a consolidated portfolio; exclude it from any "
             f"client-wide total.")

    scoped, unscoped = _split_violations(
        client, portfolio_id, store, note)

    proposal_portfolio = {
        p.proposal_id: p.portfolio_id for p in client.proposals
        if p.proposal_id is not None
    }
    proposals = _filtered(client.proposals, lambda p: p.portfolio_id == portfolio_id)
    transactions = _filtered(
        client.transactions,
        lambda t: proposal_portfolio.get(t.proposal_id) == portfolio_id)

    securities = _resolve_securities(store, portfolio, client_ref, note)
    risk_profile = _resolve(store, "risk_profiles", client.risk_profile_id)
    saa = _resolve(store, "strategic_allocations", portfolio.strategic_asset_allocation_id)

    if client.risk_profile_id is not None and risk_profile is None:
        note(Level.ERROR, "unresolved_risk_profile",
             f"RiskProfileId {client.risk_profile_id} is not in reference.json")
    if portfolio.strategic_asset_allocation_id is not None and saa is None:
        note(Level.ERROR, "unresolved_saa",
             f"StrategicAssetAllocationId {portfolio.strategic_asset_allocation_id} "
             f"is not in reference.json")

    observed = _latest_observation(portfolio)
    today = analysis_date or datetime.now(UTC).astimezone().date()
    age = (today - observed).days if observed else None
    if age is not None and age > 45:
        note(Level.WARNING, "stale_data",
             f"latest history observation is {observed} ({age} days before the analysis date "
             f"{today}); date any 'recent' statement against {observed}, not today.")

    dossier = ClientDossier(
        client_ref=client_ref,
        portfolio_id=portfolio_id,
        analysis_date=today,
        client=client,
        portfolio=portfolio,
        security_positions=portfolio.security_positions,
        account_positions=portfolio.account_positions,
        performance_history=portfolio.performance_history,
        violations=scoped,
        unscoped_violations=unscoped,
        proposals=proposals,
        transactions=transactions,
        notes=client.notes,
        tags=client.tags,
        rule_overrides=client.rule_overrides,
        risk_profile=risk_profile,
        strategic_allocation=saa,
        securities=securities,
        is_consolidated=consolidated,
        sibling_portfolio_ids=siblings,
        data_as_of=observed,
        data_age_days=age,
        diagnostics=notes,
    )
    dossier.availability = _availability(store, dossier)
    return dossier


def build_all(store: DataStore, *,
              analysis_date: date | None = None) -> Iterator[ClientDossier]:
    """Every portfolio in the export, one dossier each. Used by the tests."""
    for portfolio_id, owner in sorted(store.portfolio_owner.items()):
        yield build_dossier(store, owner, portfolio_id, analysis_date=analysis_date)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _require_client(store: DataStore, client_ref: str) -> Client:
    client = store.clients.get(client_ref)
    if client is None:
        raise DossierError(f"unknown ClientRef {client_ref!r}")
    return client


def _filtered(source: SourceList, keep: Callable[[Any], bool]) -> SourceList:
    """Narrow a client-level collection to this portfolio, preserving presence.

    If we were never given the data, that stays true after filtering. Only a
    collection we actually received can become a confirmed empty one.
    """
    items = [item for item in source.items if keep(item)]
    presence = (source.presence if source.unavailable else
                Presence.PRESENT if items else Presence.EMPTY)
    return SourceList(items, presence, source.path, source.raw_type)


def _split_violations(client: Client, portfolio_id: int, store: DataStore,
                      note: Callable[..., None]
                      ) -> tuple[SourceList, list[SuitabilityViolation]]:
    source = client.suitability_violations
    scoped: list[SuitabilityViolation] = []
    unscoped: list[SuitabilityViolation] = []
    for violation in source.items:
        pid = violation.portfolio_id
        if pid == portfolio_id:
            violation.scope = ViolationScope.PORTFOLIO
            scoped.append(violation)
        elif pid is not None and pid not in store.portfolios:
            violation.scope = ViolationScope.PORTFOLIO_NOT_IN_EXPORT
            unscoped.append(violation)
        # else: belongs to another portfolio we do have -- not this dossier's

    if unscoped:
        missing = sorted({v.portfolio_id for v in unscoped if v.portfolio_id is not None})
        note(Level.WARNING, "violations_for_absent_portfolio",
             f"{len(unscoped)} violation(s) name portfolio(s) {missing} which are not "
             f"in the export; kept at client level, not attributed to "
             f"portfolio {portfolio_id}.")

    presence = (source.presence if source.unavailable else
                Presence.PRESENT if scoped else Presence.EMPTY)
    return SourceList(scoped, presence, source.path, source.raw_type), unscoped


def _resolve(store: DataStore, attr: str, key: int | None) -> dict[str, Any] | None:
    if key is None or store.reference is None:
        return None
    return getattr(store.reference, attr).get(key)


def _resolve_securities(store: DataStore, portfolio: Portfolio, client_ref: str,
                        note: Callable[..., None]) -> dict[int, dict[str, Any]]:
    if store.reference is None:
        return {}
    resolved: dict[int, dict[str, Any]] = {}
    unresolved: list[int] = []
    for position in portfolio.security_positions:
        sid = position.security_id
        if sid is None:
            continue
        record = store.reference.securities.get(sid)
        if record is None:
            unresolved.append(sid)
        else:
            resolved[sid] = record
    if unresolved:
        note(Level.ERROR, "unresolved_securities",
             f"SecurityId(s) {sorted(set(unresolved))} held but absent from "
             f"reference.json; classification-based checks cannot cover them.")
    return resolved


def _latest_observation(portfolio: Portfolio) -> date | None:
    dates = [h.parsed_date for h in portfolio.performance_history if h.parsed_date]
    return max(dates) if dates else None


# --------------------------------------------------------------------------
# Availability: which checks Khalil can actually run
# --------------------------------------------------------------------------

def _availability(store: DataStore, d: ClientDossier) -> dict[str, Availability]:
    ok = Availability(CheckStatus.AVAILABLE)
    no_ref = CheckStatus.NO_REFERENCE_DATA
    no_src = CheckStatus.NO_SOURCE_DATA
    result: dict[str, Availability] = {}

    # suitability -- recorded violations, no reference data needed
    result[CHECK_SUITABILITY] = (
        Availability(no_src, f"SuitabilityViolations were {d.violations.presence.value} "
                             f"in the export")
        if d.violations.unavailable else ok
    )

    # concentration -- needs positions and a denominator
    amounts = [p.total_amount_in_portfolio_currency
               for p in [*d.security_positions, *d.account_positions]]
    aum = d.portfolio.aum_in_default_currency
    if d.security_positions.unavailable:
        result[CHECK_CONCENTRATION] = Availability(
            no_src, f"SecurityPositions were {d.security_positions.presence.value}")
    elif d.account_positions.unavailable:
        result[CHECK_CONCENTRATION] = Availability(
            no_src, f"AccountPositions were {d.account_positions.presence.value}")
    elif aum is None or not isfinite(aum) or aum <= 0:
        result[CHECK_CONCENTRATION] = Availability(
            no_src, "portfolio AUM must be a known, finite, positive number")
    elif any(v is None or not isfinite(v) for v in amounts):
        result[CHECK_CONCENTRATION] = Availability(
            no_src, "one or more position/account values are unavailable")
    else:
        result[CHECK_CONCENTRATION] = ok

    # allocation -- needs an SAA with at least one real min/max band
    if store.reference is None:
        result[CHECK_ALLOCATION] = Availability(no_ref, "reference.json not supplied")
    elif d.strategic_allocation is None:
        result[CHECK_ALLOCATION] = Availability(
            no_ref, f"SAA {d.portfolio.strategic_asset_allocation_id} unresolved")
    else:
        bands = [b for b in (d.strategic_allocation.get("Mappings") or [])
                 if isinstance(b, dict) and band_has_limits(b)]
        result[CHECK_ALLOCATION] = ok if bands else Availability(
            no_ref, f"SAA {d.strategic_allocation.get('Id')} defines targets but no "
                    f"min/max bands; deviations only, no breaches")

    # development -- needs at least two observations
    points = [h for h in d.performance_history if h.parsed_date and h.value is not None]
    result[CHECK_DEVELOPMENT] = ok if len(points) >= 2 else Availability(
        no_src, f"{len(points)} usable history point(s); need at least 2")

    # risk limits -- needs the client's risk profile and a portfolio volatility
    if d.risk_profile is None:
        result[CHECK_RISK_LIMITS] = Availability(
            no_ref,
            "risk profile unresolved" if d.client.risk_profile_id
            else "client has no RiskProfileId")
    elif d.portfolio.volatility is None:
        result[CHECK_RISK_LIMITS] = Availability(no_src, "portfolio Volatility is absent")
    else:
        result[CHECK_RISK_LIMITS] = ok

    # liquidity
    result[CHECK_LIQUIDITY] = (
        Availability(no_src, f"AccountPositions were {d.account_positions.presence.value}")
        if d.account_positions.unavailable else ok
    )

    # proposals
    result[CHECK_PROPOSALS] = (
        Availability(no_src, f"Proposals were {d.proposals.presence.value}")
        if d.proposals.unavailable else ok
    )

    # fund look-through -- only meaningful for held funds we have mappings for
    if store.reference is None:
        result[CHECK_LOOKTHROUGH] = Availability(no_ref, "reference.json not supplied")
    else:
        held = {p.security_id for p in d.security_positions if p.security_id}
        covered = {s for s in held if s in store.reference.lookthrough}
        result[CHECK_LOOKTHROUGH] = ok if covered else Availability(
            no_ref, "no held security has fund look-through mappings")

    # client context
    result[CHECK_CLIENT_CONTEXT] = (
        ok if (d.notes and not d.notes.unavailable) or (d.tags and not d.tags.unavailable)
        else Availability(no_src, "no usable notes or tags")
    )
    return result
