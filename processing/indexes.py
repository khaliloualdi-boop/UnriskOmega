"""M3 -- lookup indexes over reference.json.

Owner: Mohamed.

Two rules here:
  * Join on SecurityId, never ISIN. 11 ISINs in reference.json are shared by two
    securities, and 9 securities carry no ISIN at all.
  * A duplicate key is surfaced as a diagnostic, never silently overwritten.
"""

from __future__ import annotations

from typing import Any

from .contracts import Diagnostic, Level, ReferenceIndex, SourceRef

REFERENCE_SOURCE = "reference.json"

# Sections we index, with the field each record is keyed by.
_BY_ID = {
    "RiskProfiles": "risk_profiles",
    "Strategies": "strategies",
    "InvestmentServices": "investment_services",
    "EsgProfiles": "esg_profiles",
    "ProposalStatuses": "proposal_statuses",
    "AdvisoryTypes": "advisory_types",
}


def build_reference_index(
    reference_data: Any,
    diagnostics: list[Diagnostic] | None = None,
) -> ReferenceIndex:
    """Index reference.json. Raises TypeError if it is not a JSON object."""

    notes = diagnostics if diagnostics is not None else []

    def note(level: Level, code: str, message: str, path: str | None = None) -> None:
        notes.append(
            Diagnostic(level, code, message, SourceRef(source=REFERENCE_SOURCE, path=path))
        )

    if not isinstance(reference_data, dict):
        raise TypeError(
            f"reference.json must be a JSON object, got {type(reference_data).__name__}"
        )

    index = ReferenceIndex(raw=reference_data)

    def rows(section: str) -> list[dict[str, Any]]:
        value = reference_data.get(section)
        if value is None:
            note(Level.WARNING, "reference_section_missing",
                 f"{section} is absent from reference.json", section)
            return []
        if not isinstance(value, list):
            note(Level.ERROR, "reference_section_invalid",
                 f"{section} is {type(value).__name__}, expected a list", section)
            return []
        return [r for r in value if isinstance(r, dict)]

    # --- securities: by Id, plus an ISIN -> [id] map that keeps duplicates ---
    for i, sec in enumerate(rows("Securities")):
        sec_id = sec.get("Id")
        if not isinstance(sec_id, int):
            note(Level.ERROR, "security_without_id",
                 f"Securities[{i}] has no usable Id", f"Securities[{i}]")
            continue
        if sec_id in index.securities:
            note(Level.ERROR, "duplicate_security_id",
                 f"SecurityId {sec_id} defined more than once; keeping the first",
                 f"Securities[{i}]")
            continue
        index.securities[sec_id] = sec
        isin = sec.get("Isin")
        if isinstance(isin, str) and isin.strip():
            index.securities_by_isin.setdefault(isin.strip(), []).append(sec_id)

    shared = {k: v for k, v in index.securities_by_isin.items() if len(v) > 1}
    if shared:
        note(Level.WARNING, "isin_shared_by_securities",
             f"{len(shared)} ISINs map to more than one SecurityId; "
             f"resolve positions by SecurityId only", "Securities")

    # --- suitability rules: by RuleCode, always a list ---
    for i, rule in enumerate(rows("SuitabilityRules")):
        code = rule.get("RuleCode")
        if not isinstance(code, str) or not code.strip():
            note(Level.ERROR, "rule_without_code",
                 f"SuitabilityRules[{i}] has no RuleCode", f"SuitabilityRules[{i}]")
            continue
        bucket = index.rules.setdefault(code.strip(), [])
        bucket.append(rule)
        if len(bucket) > 1:
            note(Level.WARNING, "duplicate_rule_code",
                 f"RuleCode {code.strip()!r} is defined {len(bucket)} times; "
                 f"both definitions retained", f"SuitabilityRules[{i}]")

    # --- simple id-keyed sections ---
    for section, attr in _BY_ID.items():
        target: dict[int, dict[str, Any]] = getattr(index, attr)
        for i, row in enumerate(rows(section)):
            key = row.get("Id")
            if not isinstance(key, int):
                note(Level.ERROR, "record_without_id",
                     f"{section}[{i}] has no usable Id", f"{section}[{i}]")
                continue
            if key in target:
                note(Level.ERROR, f"duplicate_{attr}",
                     f"{section} Id {key} defined more than once; keeping the first",
                     f"{section}[{i}]")
                continue
            target[key] = row

    # --- strategic asset allocations and their bands ---
    for i, saa in enumerate(rows("StrategicAssetAllocations")):
        saa_id = saa.get("Id")
        if not isinstance(saa_id, int):
            note(Level.ERROR, "saa_without_id",
                 f"StrategicAssetAllocations[{i}] has no usable Id",
                 f"StrategicAssetAllocations[{i}]")
            continue
        index.strategic_allocations[saa_id] = saa
        mappings = saa.get("Mappings")
        if not isinstance(mappings, list):
            note(Level.WARNING, "saa_without_mappings",
                 f"SAA {saa_id} has no Mappings list",
                 f"StrategicAssetAllocations[{i}]")
            continue
        for j, band in enumerate(mappings):
            if not isinstance(band, dict):
                continue
            dimension = band.get("Dimension")
            category = band.get("Category")
            if not isinstance(dimension, str) or not isinstance(category, str):
                note(Level.WARNING, "saa_band_unkeyed",
                     f"SAA {saa_id} Mappings[{j}] has no Dimension/Category",
                     f"StrategicAssetAllocations[{i}].Mappings[{j}]")
                continue
            index.saa_bands[(saa_id, dimension, category)] = band

    # --- fund look-through, grouped so 48k rows are not rescanned per position ---
    for row in rows("FundUnbundlingMappings"):
        fund_id = row.get("FundSecurityId")
        if isinstance(fund_id, int):
            index.lookthrough.setdefault(fund_id, []).append(row)

    # --- recommendation lists ---
    for i, rec in enumerate(rows("RecommendationLists")):
        rec_id = rec.get("Id")
        if isinstance(rec_id, int):
            index.recommendation_lists[rec_id] = rec
        for entry in rec.get("Securities") or []:
            if isinstance(entry, dict) and isinstance(entry.get("SecurityId"), int):
                index.recommended_security_ids.add(entry["SecurityId"])

    # --- tags: keyed by name, which is how clients.json refers to them ---
    for i, tag in enumerate(rows("Tags")):
        name = tag.get("Name")
        if isinstance(name, str) and name.strip():
            index.tags.setdefault(name.strip(), tag)

    return index


def band_is_degenerate(band: dict[str, Any]) -> bool:
    """A 0..1 band constrains nothing. Report a strategy limitation, not compliance."""
    low, high = band.get("MinPercentage"), band.get("MaxPercentage")
    return low == 0.0 and high == 1.0


def band_has_limits(band: dict[str, Any]) -> bool:
    """Only 75 of 333 SAA bands carry Min/Max. The rest are targets, not limits."""
    return band.get("MinPercentage") is not None and band.get("MaxPercentage") is not None
