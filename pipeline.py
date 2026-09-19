"""Integration boundary: ClientDossier -> shared BriefingPayload/report.

Only largest-position concentration findings are implemented in this milestone.
Other checks are explicitly marked not implemented, even if inputs are available.
"""
import json
from concentration import analyze_concentration
from findings import build_concentration_finding
from processing.contracts import (
    ActionCandidate,
    BriefingPayload,
    CheckStatus,
    ClientDossier,
    DataGap,
    to_jsonable,
)
from reconciliation import reconcile_portfolio

IMPLEMENTED_FINDING_RULES = ["largest_position_concentration"]
PLANNED_CHECKS = (
    "suitability", "allocation", "development", "risk_limits", "liquidity",
    "proposals", "lookthrough", "client_context", "market_and_house_view",
    "top_five_concentration",
)


def _run_checks(dossier, largest_threshold, top_five_threshold):
    reconciliation = reconcile_portfolio(dossier)
    concentration = analyze_concentration(
        dossier, reconciliation, largest_threshold, top_five_threshold,
    )
    findings = []
    reason = None
    if concentration["status"] == "calculated":
        finding = build_concentration_finding(dossier, concentration)
        if finding is not None:
            findings.append(finding)
        status = "triggered" if findings else "not_triggered"
    elif concentration["status"] == "no_security_positions":
        status = "not_applicable"
        reason = concentration["reason"]
    else:
        status = "unavailable"
        reason = concentration["reason"]
    return {
        "reconciliation": reconciliation,
        "metrics": {"concentration": concentration},
        "checks": {
            "largest_position_concentration": {"status": status, "reason": reason},
        },
        "findings": findings,
    }


def _build_payload(dossier, result):
    check = result["checks"]["largest_position_concentration"]
    gaps = []
    if check["status"] == "unavailable":
        gaps.append(DataGap(
            "largest_position_concentration", CheckStatus.NO_SOURCE_DATA,
            check["reason"],
        ))
    for name in PLANNED_CHECKS:
        reason = "This analyzer has not been implemented in this milestone"
        availability = dossier.availability.get(name)
        if availability is not None and not availability.ok:
            reason += f"; input limitation: {availability.reason}"
        gaps.append(DataGap(name, CheckStatus.NOT_IMPLEMENTED, reason))
    findings = result["findings"]
    actions = [
        ActionCandidate(
            verb="review",
            target=f.facts["security_name"] or f"Security {f.facts['security_id']}",
            finding_ids=[f.id],
            rationale="Review single-position concentration against client objectives and applicable limits",
            prerequisites=["Confirm the client's current objectives and applicable limits"],
        )
        for f in findings
    ]
    return BriefingPayload(
        client_ref=dossier.client_ref,
        portfolio_id=dossier.portfolio_id,
        client_context={
            "name": dossier.client.display_name,
            "portfolio_number": dossier.portfolio.portfolio_nr,
            "portfolio_currency": dossier.portfolio.portfolio_currency,
            "portfolio_value": dossier.portfolio.aum_in_default_currency,
            "portfolio_value_currency": dossier.client.reporting_currency,
            "is_consolidated": dossier.is_consolidated,
            "history_as_of": dossier.data_as_of,
            "portfolio_snapshot_at": dossier.portfolio.factory_date_utc,
            "check_statuses": result["checks"],
            "diagnostics": dossier.diagnostics,
        },
        selected_findings=findings,
        action_candidates=actions,
        data_gaps=gaps,
        analysis_date=dossier.analysis_date,
    )


def build_briefing_payload(
    dossier: ClientDossier, *, largest_threshold=0.10, top_five_threshold=0.40,
) -> BriefingPayload:
    """Typed handoff for the API/AI team. No network calls or file writes."""
    result = _run_checks(dossier, largest_threshold, top_five_threshold)
    return _build_payload(dossier, result)


def analyze_portfolio(
    dossier: ClientDossier, *, largest_threshold=0.10, top_five_threshold=0.40,
) -> dict:
    """Detailed JSON-compatible report for debugging and batch analysis."""
    result = _run_checks(dossier, largest_threshold, top_five_threshold)
    result.update(
        client_ref=dossier.client_ref,
        portfolio_id=dossier.portfolio_id,
        portfolio_number=dossier.portfolio.portfolio_nr,
        input_availability=dossier.availability,
        diagnostics=dossier.diagnostics,
        source_presence={
            "security_positions": dossier.security_positions.presence,
            "account_positions": dossier.account_positions.presence,
            "violations": dossier.violations.presence,
        },
        briefing_payload=_build_payload(dossier, result),
    )
    return to_jsonable(result)

# ---------------------------------------------------------------------------
# CHATBOT INTEGRATION HELPERS
# ---------------------------------------------------------------------------

def build_chat_context_string(dossier: ClientDossier) -> str:
    """Converts a ClientDossier and its computed BriefingPayload into a 
    text context payload specifically formatted for LLM system prompts.
    """
    # 1. Generate the deterministic payload
    payload = build_briefing_payload(dossier)
    
    # 2. Extract CRM notes and qualitative fields directly from dossier object
    crm_notes = getattr(dossier.client, "notes", []) or getattr(dossier, "notes", [])
    proposals = getattr(dossier, "proposals", [])
    violations = getattr(dossier, "violations", [])

    # 3. Serialize payload and extra data into a readable string for GPT
    payload_dict = to_jsonable(payload)
    
    context_text = f"""
=== CLIENT BRIEFING PAYLOAD ===
{json.dumps(payload_dict, indent=2)}

=== CRM NOTES & COMMUNICATIONS ===
{json.dumps(to_jsonable(crm_notes), indent=2)}

=== OPEN PROPOSALS ===
{json.dumps(to_jsonable(proposals), indent=2)}

=== ACTIVE VIOLATIONS ===
{json.dumps(to_jsonable(violations), indent=2)}
"""
    return context_text
