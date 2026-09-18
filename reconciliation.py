"""Reconcile normalized portfolio values before using them in analytics."""
from math import fsum, isfinite

from processing.contracts import ClientDossier


def reconcile_portfolio(dossier: ClientDossier, tolerance=0.01):
    """Compare supplied values; missing collections never silently become zero.

    Default-currency AUM is interpreted in the client's ReportingCurrency.
    Without matching PortfolioCurrency we cannot compare values without FX.
    """
    if not isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    portfolio = dossier.portfolio
    recorded = portfolio.aum_in_default_currency
    missing = []
    for name, collection in (
        ("SecurityPositions", dossier.security_positions),
        ("AccountPositions", dossier.account_positions),
    ):
        if collection.unavailable:
            missing.append(f"{name}: {collection.presence.value}")
    currency = dossier.client.reporting_currency
    if not currency or not portfolio.portfolio_currency:
        missing.append("Portfolio and reporting currencies must be known")
    elif currency != portfolio.portfolio_currency:
        missing.append("Portfolio/reporting currencies differ; FX conversion is unavailable")
    positions = [*dossier.security_positions, *dossier.account_positions]
    amounts = [p.total_amount_in_portfolio_currency for p in positions]
    if recorded is None or not isfinite(recorded):
        missing.append("Recorded portfolio value is unavailable")
    if any(v is None or not isfinite(v) for v in amounts):
        missing.append("One or more position/account values are unavailable")
    result = {
        "portfolio": portfolio.portfolio_nr,
        "currency": currency,
        "status": "incomplete",
        "difference": None,
        "missing_inputs": missing,
    }
    if missing:
        return result
    try:
        total = fsum(amounts)
    except OverflowError:
        result["missing_inputs"] = ["Holdings total exceeds the supported numeric range"]
        return result
    difference = total - recorded
    if not isfinite(difference):
        result["missing_inputs"] = ["Reconciliation exceeds the supported numeric range"]
        return result
    result.update(
        status="matched" if abs(difference) <= tolerance else "mismatch",
        recorded_value=recorded,
        holdings_total=total,
        difference=round(difference, 2),
    )
    return result
