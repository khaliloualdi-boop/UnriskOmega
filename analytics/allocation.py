"""Allocation and diversification metrics for one portfolio.

Answers "what is this portfolio actually made of?" along the same coarse
buckets the Strategic Asset Allocation (SAA) targets are set against, so the
output drops straight into the drift-vs-target check that follows.

Design rules (confirmed against the case-study data reference):
  * Join holdings to the security master by ``SecurityId`` -- never ISIN
    (one ISIN can be several currency share classes).
  * Classify against the ``SAA_*`` fields on each security, not the granular
    ``AssetClassName`` etc.: only the ``SAA_*`` buckets match SAA target
    category names.
  * Weights come from ``TotalAmountInPortfolioCurrency`` (the source-reported
    ``PortfolioValuePercentage`` is kept only as cross-check evidence).
  * Cash/account positions (including crypto, whose ticker sits in ``Currency``
    but whose ``TotalAmountInPortfolioCurrency`` is a real CHF number) roll into
    the ``Liquidity`` asset-class bucket, matching the SAA's Liquidity target.
  * Nothing here emits IBANs or any account identifier.
  * Pure standard library; missing data yields ``status="unavailable"`` or an
    explicit ``Unclassified`` bucket, never a fabricated number.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

LIQUIDITY_BUCKET = "Liquidity"
UNCLASSIFIED = "Unclassified"
NOT_CLASSIFIED = "Not classified"
OTHER_CURRENCY = "Andere"

_CURRENCY_GROUP = {
    "CHF": "Swiss francs",
    "EUR": "Euro",
    "USD": "US-Dollar",
}
_CRYPTO = {"BTC", "ETH", "DOT", "SOL", "SHIB"}

# Which security field feeds each SAA dimension.
_SAA_FIELD = {
    "asset_class": "SAA_AssetClassName",
    "currency_group": "SAA_CurrencyGroupName",
    "country_group": "SAA_CountryGroupName",
    "industry": "SAA_IndustryName",
}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _bucket(records: Iterable[tuple[str, float]]) -> dict[str, float]:
    """Sum amounts by category label."""
    out: dict[str, float] = {}
    for label, amount in records:
        out[label] = out.get(label, 0.0) + amount
    return out


def _as_weights(amounts: dict[str, float], denom: float) -> dict[str, float]:
    if denom == 0:
        return {}
    return {k: v / denom for k, v in sorted(amounts.items(), key=lambda kv: -kv[1])}


def classify_positions(portfolio: Any, reference: Any) -> list[dict[str, Any]]:
    """One row per security position, joined to the security master by id.

    Amounts are in portfolio currency. A position whose id does not resolve is
    kept with ``resolved=False`` and ``Unclassified`` buckets so coverage stays
    honest rather than silently dropping it.
    """
    rows: list[dict[str, Any]] = []
    for p in portfolio.security_positions:
        amount = _finite(getattr(p, "total_amount_in_portfolio_currency", None))
        if amount is None:
            continue
        sec = reference.security(getattr(p, "security_id", None)) if reference else None
        buckets = {}
        for dim, field in _SAA_FIELD.items():
            val = sec.get(field) if sec else None
            buckets[dim] = val if (isinstance(val, str) and val.strip()) else UNCLASSIFIED
        rows.append({
            "security_id": getattr(p, "security_id", None),
            "security_name": getattr(p, "security_name", None),
            "isin": getattr(p, "isin", None),
            "currency": getattr(p, "currency", None),
            "instrument_type": sec.get("SecurityTypeName") if sec else None,
            "amount": amount,
            "resolved": sec is not None,
            "buckets": buckets,
            "source_weight": _finite(getattr(p, "portfolio_value_percentage", None)),
        })
    return rows


def _invalid_position_count(portfolio: Any) -> int:
    return sum(
        _finite(getattr(position, "total_amount_in_portfolio_currency", None)) is None
        for position in portfolio.security_positions
    )


def _account_exposure(portfolio: Any) -> tuple[float, bool, float, dict[str, float]]:
    """Return total accounts, crypto flag/value, and currency-group amounts.

    Crypto reuses the cash-account shape in the source (its ticker stands in for
    Currency), so its ``TotalAmountInPortfolioCurrency`` is a real number and
    is summed normally -- it is never treated as a tradable FX code. We surface
    the crypto value separately so it stays visible rather than hidden in the
    Liquidity bucket it rolls into.
    """
    total = 0.0
    crypto_value = 0.0
    currency_amounts: dict[str, float] = {}
    for a in getattr(portfolio, "account_positions", []) or []:
        amt = _finite(getattr(a, "total_amount_in_portfolio_currency", None))
        if amt is None:
            continue
        total += amt
        cur = getattr(a, "currency", None)
        code = cur.strip().upper() if isinstance(cur, str) and cur.strip() else None
        category = _CURRENCY_GROUP.get(code, OTHER_CURRENCY)
        currency_amounts[category] = currency_amounts.get(category, 0.0) + amt
        if code in _CRYPTO:
            crypto_value += amt
    return total, crypto_value > 0, crypto_value, currency_amounts


def _diversification(weights: dict[str, float]) -> dict[str, Any]:
    """Herfindahl concentration on the security book (long weights only)."""
    longs = [w for w in weights.values() if w > 0]
    hhi = sum(w * w for w in longs) if longs else None
    eff_n = (1.0 / hhi) if hhi else None
    return {"hhi": hhi, "effective_holdings": eff_n, "holdings": len(weights)}


def analyze_allocation(portfolio: Any, reference: Any) -> dict[str, Any]:
    """Allocation breakdowns + diversification + coverage for one portfolio."""
    if reference is None:
        return {"status": "unavailable", "reason": "reference.json not supplied; cannot classify holdings."}
    if getattr(portfolio.security_positions, "unavailable", False):
        return {
            "status": "unavailable",
            "reason": "SecurityPositions were not provided by the source (absent/null/invalid).",
        }
    accounts = getattr(portfolio, "account_positions", None)
    if accounts is None or getattr(accounts, "unavailable", False):
        return {
            "status": "unavailable",
            "reason": "AccountPositions were not provided by the source (absent/null/invalid).",
        }
    invalid_positions = _invalid_position_count(portfolio)
    invalid_accounts = sum(
        _finite(getattr(account, "total_amount_in_portfolio_currency", None)) is None
        for account in accounts
    )
    if invalid_positions or invalid_accounts:
        return {
            "status": "unavailable",
            "reason": (
                f"Invalid or missing values in {invalid_positions} security position(s) "
                f"and {invalid_accounts} account position(s)."
            ),
        }

    rows = classify_positions(portfolio, reference)
    if not rows:
        return {"status": "no_security_positions", "reason": "No usable security positions on this portfolio."}

    securities_total = sum(r["amount"] for r in rows)
    accounts_total, has_crypto, crypto_value, account_currencies = _account_exposure(portfolio)
    positions_total = securities_total + accounts_total
    aum = _finite(getattr(portfolio, "aum_in_default_currency", None))
    has_short = any(r["amount"] < 0 for r in rows)

    # --- per-security weights on the securities book (for diversification) ---
    security_amounts: dict[str, float] = {}
    for index, row in enumerate(rows):
        key = str(row["security_id"]) if row["security_id"] is not None else f"unknown:{index}"
        security_amounts[key] = security_amounts.get(key, 0.0) + row["amount"]
    sec_weights = _as_weights(security_amounts, securities_total)

    # --- asset-class allocation over ALL positions, cash -> Liquidity ---
    ac_amounts = _bucket((r["buckets"]["asset_class"], r["amount"]) for r in rows)
    if accounts_total:
        ac_amounts[LIQUIDITY_BUCKET] = ac_amounts.get(LIQUIDITY_BUCKET, 0.0) + accounts_total
    asset_class = _as_weights(ac_amounts, positions_total)

    # --- currency / country / industry over ALL positions -----------------
    # Cash must remain in every dimension. Otherwise security weights are
    # overstated whenever a portfolio carries cash and SAA drift is distorted.
    def dim_alloc(dim: str) -> dict[str, Any]:
        amounts = _bucket((r["buckets"][dim], r["amount"]) for r in rows)
        if accounts_total:
            if dim == "currency_group":
                for category, amount in account_currencies.items():
                    amounts[category] = amounts.get(category, 0.0) + amount
            else:
                amounts[NOT_CLASSIFIED] = amounts.get(NOT_CLASSIFIED, 0.0) + accounts_total
        classified = sum(v for k, v in amounts.items() if k != UNCLASSIFIED)
        return {
            "weights": _as_weights(amounts, positions_total),
            "classified_share": (classified / positions_total) if positions_total else None,
        }

    top = max(rows, key=lambda r: r["amount"])
    coverage = {
        "positions_total": positions_total,
        "securities_total": securities_total,
        "accounts_total": accounts_total,
        "aum": aum,
        # How well the summed positions reconcile to reported AUM (1.0 = exact).
        "positions_vs_aum": (positions_total / aum) if aum else None,
        "cash_share": (accounts_total / positions_total) if positions_total else None,
        "asset_class_classified_share": (
            sum(v for k, v in ac_amounts.items() if k not in (UNCLASSIFIED,))
            / positions_total if positions_total else None
        ),
        "has_crypto_account": has_crypto,
        "crypto_value_chf": crypto_value,
        "crypto_share": (crypto_value / positions_total) if positions_total else None,
        "has_short_position": has_short,
    }

    return {
        "status": "calculated",
        "portfolio_id": getattr(portfolio, "portfolio_id", None),
        "portfolio_number": getattr(portfolio, "portfolio_nr", None),
        "currency": getattr(portfolio, "portfolio_currency", None),
        "allocation": {
            "asset_class": {"basis": "all positions incl. cash", "weights": asset_class},
            "currency_group": {"basis": "all positions incl. cash", **dim_alloc("currency_group")},
            "country_group": {"basis": "all positions incl. cash", **dim_alloc("country_group")},
            "industry": {"basis": "all positions incl. cash", **dim_alloc("industry")},
        },
        "diversification": {
            **_diversification(sec_weights),
            "largest_holding": {
                "security_id": top["security_id"],
                "security_name": top["security_name"],
                "weight_of_securities": top["amount"] / securities_total if securities_total else None,
            },
        },
        "coverage": coverage,
    }
