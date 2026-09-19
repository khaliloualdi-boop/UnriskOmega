"""Search-ready views for the news module: ranked holdings and accounts.

Neither block computes new financial metrics -- both reuse
``allocation.classify_positions`` and the raw account positions, so nothing is
duplicated. The point is to expose, per instrument and per currency, exactly the
fields the news/scraping module needs to target real companies, sectors,
regions and currencies.

Firm rules from the data spec:
  * Join to the security master by ``SecurityId`` (done in classify_positions).
  * Never fabricate a ticker, issuer or alias -- there is no distinct issuer
    field in the source, so ``issuer_name`` reuses the instrument name and says
    so; the news module resolves the real issuer downstream.
  * Accounts: distinguish ordinary cash from crypto (ticker in ``Currency``),
    and never emit IBANs or account names.
  * Unknown value = null, never 0. Weights carry an explicit denominator.
"""
from __future__ import annotations

import math
from typing import Any

from .allocation import classify_positions

# ISO-4217 codes present in, or reasonably expected from, the source export.
# Crypto is an explicit allow-list: an unfamiliar fiat code must never be
# silently relabelled as a crypto asset.
_ISO = {"CHF", "EUR", "USD", "GBP", "JPY", "AUD", "CAD", "SGD", "HKD", "NOK",
        "SEK", "DKK", "CNY", "NZD", "ZAR", "PLN", "CZK", "HUF", "TRY", "MXN"}
_CRYPTO = {"BTC", "ETH", "DOT", "SOL", "SHIB"}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _weight(amount: float, denom: float | None) -> float | None:
    return (amount / denom) if (denom and denom != 0) else None


def top_holdings(portfolio: Any, reference: Any, *, limit: int | None = None) -> dict[str, Any]:
    """Positions ranked by value, each carrying the fields the news module needs."""
    if reference is None:
        return {"status": "unavailable", "reason": "reference.json not supplied; cannot classify holdings."}
    if getattr(portfolio.security_positions, "unavailable", False):
        return {"status": "unavailable",
                "reason": "SecurityPositions were not provided by the source (absent/null/invalid)."}
    if limit is not None and limit < 0:
        return {"status": "unavailable", "reason": "limit must be non-negative."}
    invalid_positions = sum(
        _finite(getattr(position, "total_amount_in_portfolio_currency", None)) is None
        for position in portfolio.security_positions
    )
    if invalid_positions:
        return {
            "status": "unavailable",
            "reason": f"{invalid_positions} security position(s) have invalid or missing values.",
        }
    rows = classify_positions(portfolio, reference)
    if not rows:
        return {"status": "no_security_positions", "reason": "No usable security positions on this portfolio."}

    securities_total = sum(r["amount"] for r in rows)
    accounts = getattr(portfolio, "account_positions", None)
    account_values = (
        [_finite(getattr(account, "total_amount_in_portfolio_currency", None)) for account in accounts]
        if accounts is not None and not getattr(accounts, "unavailable", False)
        else None
    )
    if account_values is not None and all(value is not None for value in account_values):
        positions_total = securities_total + sum(value for value in account_values if value is not None)
        denom = positions_total if positions_total > 0 else None
        weight_basis = "sum of all security and account positions"
    else:
        denom = None
        weight_basis = "unavailable because account positions are missing or invalid"
    ranked = sorted(rows, key=lambda r: -r["amount"])
    kept = ranked[:limit] if limit is not None else ranked

    holdings = [{
        "security_id": r["security_id"],
        "isin": r.get("isin"),
        "name": r["security_name"],
        "issuer_name": r["security_name"],          # no distinct issuer field in source
        "instrument_type": r.get("instrument_type"),
        "value": r["amount"],
        "currency": r.get("currency"),
        "weight_of_total": _weight(r["amount"], denom),
        "asset_class": r["buckets"]["asset_class"],
        "sector": r["buckets"]["industry"],
        "region": r["buckets"]["country_group"],
        "classified": r["resolved"],
    } for r in kept]

    return {
        "status": "calculated",
        "as_of": getattr(portfolio, "factory_date_utc", None),
        "source": "clients.json (positions) + reference.json (classification)",
        "weight_basis": weight_basis,
        "count": len(rows),
        "shown": len(kept),
        "omitted": len(rows) - len(kept),
        "issuer_name_note": "No distinct issuer field in source; issuer_name reuses the instrument name "
                            "and is not a verified issuer -- the news module must resolve the issuer.",
        "holdings": holdings,
    }


def accounts_by_currency(portfolio: Any) -> dict[str, Any]:
    """Cash/crypto accounts aggregated by currency code (no IBANs, no names)."""
    accounts = getattr(portfolio, "account_positions", None)
    if accounts is None or getattr(accounts, "unavailable", False):
        return {"status": "unavailable", "reason": "AccountPositions not provided by the source."}

    agg: dict[str, dict[str, Any]] = {}
    invalid_amounts = 0
    for a in accounts or []:
        amt = _finite(getattr(a, "total_amount_in_portfolio_currency", None))
        if amt is None:
            invalid_amounts += 1
            continue
        raw = getattr(a, "currency", None)
        code = raw.strip().upper() if isinstance(raw, str) and raw.strip() else "UNKNOWN"
        category = "cash" if code in _ISO else ("crypto" if code in _CRYPTO else "unknown")
        entry = agg.setdefault(code, {"currency": code, "category": category, "value": 0.0})
        entry["value"] += amt

    if invalid_amounts:
        return {
            "status": "unavailable",
            "reason": f"{invalid_amounts} account position(s) have invalid or missing values.",
        }
    if not agg:
        return {"status": "no_accounts", "reason": "No usable account positions on this portfolio."}

    accounts_total = sum(e["value"] for e in agg.values())
    security_positions = getattr(portfolio, "security_positions", None)
    security_values = (
        [_finite(getattr(position, "total_amount_in_portfolio_currency", None)) for position in security_positions]
        if security_positions is not None and not getattr(security_positions, "unavailable", False)
        else None
    )
    if security_values is not None and all(value is not None for value in security_values):
        positions_total = accounts_total + sum(value for value in security_values if value is not None)
        denom = positions_total if positions_total > 0 else None
        weight_basis = "sum of all security and account positions"
    else:
        denom = None
        weight_basis = "unavailable because security positions are missing or invalid"
    entries = sorted(agg.values(), key=lambda e: -e["value"])
    for e in entries:
        e["weight_of_total"] = _weight(e["value"], denom)

    cash_total = sum(e["value"] for e in entries if e["category"] == "cash")
    crypto_total = sum(e["value"] for e in entries if e["category"] == "crypto")
    return {
        "status": "calculated",
        "as_of": getattr(portfolio, "factory_date_utc", None),
        "source": "clients.json (AccountPositions)",
        "note": "IBANs and account names are excluded. Crypto tickers stand in for Currency; "
                "their value in portfolio currency is real and summable, but they are not tradable fiat.",
        "weight_basis": weight_basis,
        "by_currency": entries,
        "totals": {
            "cash_ex_crypto": cash_total,
            "crypto": crypto_total,
            "cash_share_ex_crypto": _weight(cash_total, denom),
            "crypto_share": _weight(crypto_total, denom),
        },
    }
