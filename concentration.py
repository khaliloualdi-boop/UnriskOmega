"""Position-level screening, not a client-specific investment-limit check."""
from math import fsum, isfinite

from processing.contracts import ClientDossier


def analyze_concentration(
    dossier: ClientDossier,
    reconciliation,
    largest_threshold=0.10,
    top_five_threshold=0.40,
):
    for threshold in (largest_threshold, top_five_threshold):
        if not isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Concentration thresholds must be fractions between 0 and 1")
    if reconciliation["status"] != "matched":
        return {"status": "unavailable", "reason": "Portfolio reconciliation needs review"}
    availability = dossier.check("concentration")
    if not availability.ok:
        return {"status": "unavailable", "reason": availability.reason}
    portfolio = dossier.portfolio
    positions = list(dossier.security_positions)
    if not positions:
        return {
            "status": "no_security_positions",
            "reason": "The source explicitly contains no security positions",
        }
    ids = [p.security_id for p in positions]
    if any(sid is None for sid in ids) or len(ids) != len(set(ids)):
        return {"status": "unavailable", "reason": "Security IDs are missing or duplicated"}
    if any(p.total_amount_in_portfolio_currency < 0 for p in positions):
        return {
            "status": "unavailable",
            "reason": "Short security positions require a separate concentration policy",
        }
    positions.sort(key=lambda p: (-p.total_amount_in_portfolio_currency, p.security_id))
    largest = positions[0]
    weight = largest.total_amount_in_portfolio_currency / portfolio.aum_in_default_currency
    top_five = fsum(p.total_amount_in_portfolio_currency for p in positions[:5])
    top_five_weight = top_five / portfolio.aum_in_default_currency
    if not isfinite(weight) or not isfinite(top_five_weight):
        return {"status": "unavailable", "reason": "Concentration exceeds the supported numeric range"}
    return {
        "status": "calculated",
        "portfolio_id": dossier.portfolio_id,
        "largest_security_id": largest.security_id,
        "largest_security_name": largest.security_name,
        "largest_position_weight": weight,
        "largest_position_above_threshold": weight > largest_threshold,
        "top_five_weight": top_five_weight,
        "top_five_above_threshold": top_five_weight > top_five_threshold,
        "largest_threshold": largest_threshold,
        "top_five_threshold": top_five_threshold,
    }
