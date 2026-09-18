"""Build shared Finding objects from deterministic analytics."""
from dataclasses import replace

from processing.contracts import ClientDossier, Finding, Section, SourceRef


def _evidence(record, field_name, normalized_value):
    # Preserve the source spelling/type even when the loader normalized it.
    value = record.raw.get(field_name, normalized_value)
    return replace(
        record.source,
        field=field_name,
        path=f"{record.source.path}.{field_name}",
        value=value,
    )


def build_concentration_finding(dossier: ClientDossier, concentration):
    if concentration["status"] != "calculated":
        raise ValueError("A calculated concentration result is required")
    if not concentration["largest_position_above_threshold"]:
        return None
    position = next(
        p for p in dossier.security_positions
        if p.security_id == concentration["largest_security_id"]
    )
    portfolio = dossier.portfolio
    weight_pct = round(concentration["largest_position_weight"] * 100, 1)
    threshold_pct = round(concentration["largest_threshold"] * 100, 1)
    name = position.security_name or f"Security {position.security_id}"
    name_field = "SecurityName" if position.security_name else "SecurityId"
    name_value = position.security_name or position.security_id
    return Finding(
        id=f"{dossier.client_ref}:{dossier.portfolio_id}:concentration:{position.security_id}",
        analyzer="concentration",
        section=Section.HEALTH,
        kind="risk",
        headline=f"{name} represents {weight_pct:.1f}% of the portfolio",
        facts={
            "security_name": position.security_name,
            "security_id": position.security_id,
            "weight_pct": weight_pct,
            "screening_threshold_pct": threshold_pct,
            "threshold_type": "project_screening_rule",
        },
        calculation={
            "formula": "position_value / portfolio_value * 100",
            "position_value": position.total_amount_in_portfolio_currency,
            "portfolio_value": portfolio.aum_in_default_currency,
            "currency": portfolio.portfolio_currency,
            "weight_fraction": concentration["largest_position_weight"],
            "aum_currency_basis": "client ReportingCurrency; must equal PortfolioCurrency",
        },
        evidence=[
            _evidence(position, name_field, name_value),
            _evidence(position, "TotalAmountInPortfolioCurrency", position.total_amount_in_portfolio_currency),
            _evidence(portfolio, "AssetsUnderManagementInDefaultCurrency", portfolio.aum_in_default_currency),
            _evidence(portfolio, "PortfolioCurrency", portfolio.portfolio_currency),
            _evidence(dossier.client, "ReportingCurrency", dossier.client.reporting_currency),
            SourceRef(
                source="analysis_configuration",
                path="concentration.largest_threshold",
                field="largest_threshold",
                value=concentration["largest_threshold"],
            ),
        ],
        selection_reason="Largest security position exceeds the configured screening threshold",
    )
