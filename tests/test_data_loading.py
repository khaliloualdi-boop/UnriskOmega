"""M1-M5 acceptance tests. Owner: Mohamed.

Run:  uv run pytest -q          (or: pytest -q)

These mirror the "minimum meaningful checks" in the processing plan. Each test
is named for the failure it prevents, not for the function it calls.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from processing.contracts import CheckStatus, Presence, to_jsonable
from processing.dossier import DossierError, build_all, build_dossier, select_portfolio
from processing.loader import (
    DUPLICATE_KEEP_FIRST,
    DUPLICATE_REPLACE,
    InputValidationError,
    load_store,
    parse_timestamp,
)

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DATE = date(2026, 9, 19)


@pytest.fixture(scope="session")
def clients_data():
    return json.loads((ROOT / "clients.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def reference_data():
    return json.loads((ROOT / "reference.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def store(clients_data, reference_data):
    return load_store(clients_data, reference_data)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def test_all_clients_and_portfolios_load(store):
    assert len(store.clients) == 47
    assert len(store.portfolios) == 57
    assert not store.errors(), [d.message for d in store.errors()]


def test_every_portfolio_produces_a_dossier(store):
    built = list(build_all(store, analysis_date=ANALYSIS_DATE))
    assert len(built) == 57


def test_reference_data_is_indexed(store):
    ref = store.reference
    assert len(ref.securities) == 504
    assert len(ref.strategic_allocations) == 16
    assert len(ref.saa_bands) == 333


# --------------------------------------------------------------------------
# The core distinction: absent vs null vs empty
# --------------------------------------------------------------------------

def test_null_collection_is_not_a_confirmed_empty_one(store):
    """CASE-016 has SuitabilityViolations: null. Reporting her as compliant is
    the single worst failure this pipeline can produce."""
    holly = store.clients["CASE-016"]
    assert holly.suitability_violations.presence is Presence.NULL
    assert holly.suitability_violations.unavailable
    assert not holly.suitability_violations.is_known_empty


def test_empty_collection_is_a_confirmed_empty_one(store):
    """CASE-023 has [] -- the same zero, the opposite meaning."""
    frodo = store.clients["CASE-023"]
    assert frodo.suitability_violations.presence is Presence.EMPTY
    assert frodo.suitability_violations.is_known_empty
    assert not frodo.suitability_violations.unavailable


def test_absent_key_is_distinguished_from_empty_list(store):
    """8 portfolios have no SecurityPositions key at all."""
    absent = [p for p in store.portfolios.values()
              if p.security_positions.presence is Presence.ABSENT]
    assert len(absent) == 8
    assert all(p.security_positions.unavailable for p in absent)


def test_missing_numbers_are_none_never_zero(store):
    missing = [p for p in store.portfolios.values() if p.volatility is None]
    assert len(missing) == 7
    assert all(p.volatility is None for p in missing)


# --------------------------------------------------------------------------
# Wrong types
# --------------------------------------------------------------------------

def test_na_placeholder_collection_is_rejected_loudly(clients_data):
    """clients_cleaned.json replaces nulls with "N/A". A truthy string is not a
    list, and must not be silently accepted."""
    poisoned = copy.deepcopy(clients_data[:3])
    poisoned[0]["SuitabilityViolations"] = "N/A"
    store = load_store(poisoned)

    violations = store.clients[poisoned[0]["ClientRef"]].suitability_violations
    assert violations.presence is Presence.INVALID
    assert violations.unavailable
    assert not violations.is_known_empty
    codes = {d.code for d in store.diagnostics}
    assert "invalid_collection_type" in codes
    assert "placeholder_source_suspected" in codes


def test_list_of_unusable_entries_is_invalid_not_empty(clients_data):
    poisoned = copy.deepcopy(clients_data[:1])
    poisoned[0]["ClientNotes"] = ["not an object", 42]
    store = load_store(poisoned)
    notes = store.clients[poisoned[0]["ClientRef"]].notes
    assert notes.presence is Presence.INVALID
    assert not notes.is_known_empty


def test_bad_input_shape_raises_one_clear_error():
    with pytest.raises(InputValidationError):
        load_store("not json")
    with pytest.raises(InputValidationError):
        load_store({"unexpected": []})


# --------------------------------------------------------------------------
# M5: new input files, 40 + 7
# --------------------------------------------------------------------------

def test_forty_plus_seven_uses_the_same_code_path(clients_data, reference_data):
    first = load_store(clients_data[:40], reference_data)
    assert len(first.clients) == 40

    full = load_store(clients_data, reference_data)
    assert len(full.clients) == 47

    later = load_store(clients_data[40:], reference_data)
    assert len(later.clients) == 7

    # a client loaded in the small batch is identical to the same client in the
    # full batch -- no code path depends on which file it arrived in
    ref = clients_data[45]["ClientRef"]
    assert to_jsonable(later.clients[ref]) == to_jsonable(full.clients[ref])


def test_duplicate_client_is_refused_by_default(clients_data):
    doubled = clients_data[:2] + clients_data[:1]
    with pytest.raises(InputValidationError) as excinfo:
        load_store(doubled)
    assert clients_data[0]["ClientRef"] in str(excinfo.value)


def test_duplicate_client_policies_are_explicit(clients_data):
    doubled = clients_data[:2] + clients_data[:1]
    for policy in (DUPLICATE_REPLACE, DUPLICATE_KEEP_FIRST):
        store = load_store(doubled, on_duplicate=policy)
        assert len(store.clients) == 2
        assert any(d.code == "duplicate_client" for d in store.diagnostics)


# --------------------------------------------------------------------------
# Joins
# --------------------------------------------------------------------------

def test_repeated_isins_are_never_collapsed(store):
    shared = {i: ids for i, ids in store.reference.securities_by_isin.items()
              if len(ids) > 1}
    assert len(shared) == 11
    for ids in shared.values():
        assert len(set(ids)) == len(ids)
        for sid in ids:
            assert sid in store.reference.securities


def test_duplicate_rule_code_keeps_both_definitions(store):
    duplicated = [c for c, defs in store.reference.rules.items() if len(defs) > 1]
    assert duplicated
    for code in duplicated:
        assert len(store.reference.rule(code)) > 1


def test_every_held_security_resolves_by_id(store):
    for dossier in build_all(store, analysis_date=ANALYSIS_DATE):
        held = {p.security_id for p in dossier.security_positions if p.security_id}
        assert held <= set(store.reference.securities)


# --------------------------------------------------------------------------
# Dossier scoping
# --------------------------------------------------------------------------

def test_violations_for_absent_portfolios_stay_at_client_level(store):
    d = build_dossier(store, "CASE-038", 141512, analysis_date=ANALYSIS_DATE)
    assert d.unscoped_violations
    assert all(v.portfolio_id not in store.portfolios for v in d.unscoped_violations)
    assert all(v.portfolio_id == 141512 for v in d.violations)


def test_consolidated_portfolio_is_flagged_not_merged(store):
    d = build_dossier(store, "CASE-038", 141512, analysis_date=ANALYSIS_DATE)
    assert d.is_consolidated is True
    assert 140557 in d.sibling_portfolio_ids
    assert any(x.code == "consolidated_portfolio" for x in d.diagnostics)


def test_default_selection_avoids_the_consolidated_portfolio(store):
    assert select_portfolio(store, "CASE-038") != 141512


def test_crypto_account_keeps_its_identity(store):
    d = build_dossier(store, "CASE-001", analysis_date=ANALYSIS_DATE)
    currencies = {a.currency for a in d.account_positions}
    assert "BTC" in currencies


def test_unknown_client_or_portfolio_fails_by_name(store):
    with pytest.raises(DossierError):
        build_dossier(store, "CASE-999")
    with pytest.raises(DossierError):
        build_dossier(store, "CASE-016", 999999)
    with pytest.raises(DossierError, match="belongs to CASE-038"):
        build_dossier(store, "CASE-016", 141512)


# --------------------------------------------------------------------------
# Availability: missing must never look like passing
# --------------------------------------------------------------------------

def test_unavailable_check_carries_a_reason(store):
    d = build_dossier(store, "CASE-016", 315, analysis_date=ANALYSIS_DATE)
    suitability = d.check("suitability")
    assert suitability.status is CheckStatus.NO_SOURCE_DATA
    assert "null" in (suitability.reason or "")


def test_no_check_is_available_without_its_data(store):
    for d in build_all(store, analysis_date=ANALYSIS_DATE):
        if d.security_positions.unavailable:
            assert not d.check("concentration").ok
        if d.violations.unavailable:
            assert not d.check("suitability").ok


def test_reference_free_load_disables_dependent_checks(clients_data):
    store = load_store(clients_data)
    assert store.reference is None
    # CASE-023 has an explicitly empty violations list, so the suitability check
    # is runnable with no reference data at all; allocation is not.
    d = build_dossier(store, "CASE-023", analysis_date=ANALYSIS_DATE)
    assert d.check("allocation").status is CheckStatus.NO_REFERENCE_DATA
    assert d.check("suitability").ok      # recorded violations need no reference


def test_stale_data_is_surfaced_not_hidden(store):
    d = build_dossier(store, "CASE-016", 315, analysis_date=ANALYSIS_DATE)
    assert d.data_as_of == date(2026, 7, 1)
    assert d.data_age_days == 80
    assert any(x.code == "stale_data" for x in d.diagnostics)


# --------------------------------------------------------------------------
# Dates and determinism
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("2021-10-01", date(2021, 10, 1)),
    ("2026-09-05T09:16:32", date(2026, 9, 5)),
    ("2023-11-06T23:00:00+01:00", date(2023, 11, 6)),
    ("2026-08-15T09:53:29.943Z", date(2026, 8, 15)),
    ("2026-07-21T05:53:53.9Z", date(2026, 7, 21)),
    ("2023-11-11T16:05:42.3970767Z", date(2023, 11, 11)),   # .NET, 7 digits
    ("N/A", None), ("", None), (None, None), (12345, None),
])
def test_every_date_shape_in_the_export_parses(text, expected):
    assert parse_timestamp(text) == expected


def test_all_history_dates_parse(store):
    unparsed = [h for p in store.portfolios.values()
                for h in p.performance_history
                if h.date and h.parsed_date is None]
    assert not unparsed


def test_same_input_gives_the_same_output(clients_data, reference_data):
    a = load_store(clients_data, reference_data)
    b = load_store(clients_data, reference_data)
    da = build_dossier(a, "CASE-038", 141512, analysis_date=ANALYSIS_DATE)
    db = build_dossier(b, "CASE-038", 141512, analysis_date=ANALYSIS_DATE)
    assert to_jsonable(da.availability) == to_jsonable(db.availability)
    assert to_jsonable(da.violations) == to_jsonable(db.violations)


def test_output_is_json_serializable_with_null_not_nan(store):
    d = build_dossier(store, "CASE-001", analysis_date=ANALYSIS_DATE)
    blob = json.dumps(to_jsonable(d.availability))
    assert "NaN" not in blob and '"N/A"' not in blob
