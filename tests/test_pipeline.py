"""Integration and regression tests for Mohamed's data and Khalil's analytics."""
import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from pipeline import analyze_portfolio, build_briefing_payload
from processing.contracts import Finding, Presence, Section, to_jsonable
from processing.dossier import build_all, build_dossier
from processing.loader import load_store
from reconciliation import reconcile_portfolio

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 19)


@pytest.fixture(scope="module")
def raw_data():
    return json.loads((ROOT / "clients.json").read_text())


@pytest.fixture(scope="module")
def reference_data():
    return json.loads((ROOT / "reference.json").read_text())


@pytest.fixture(scope="module")
def store(raw_data, reference_data):
    return load_store(raw_data, reference_data)


def dossier_for(store, client_ref, portfolio_id=None):
    return build_dossier(store, client_ref, portfolio_id, analysis_date=DAY)


def mutated_holly(raw_data, mutate):
    client = copy.deepcopy(next(c for c in raw_data if c["ClientRef"] == "CASE-016"))
    mutate(client["Portfolios"][0])
    store = load_store([client])
    return store, dossier_for(store, "CASE-016", 315)


@pytest.mark.parametrize("client_ref,pid,status", [
    ("CASE-016", 315, "triggered"),
    ("CASE-004", None, "not_triggered"),
    ("CASE-001", None, "unavailable"),
    ("CASE-023", None, "unavailable"),
])
def test_distinct_check_outcomes(store, client_ref, pid, status):
    report = analyze_portfolio(dossier_for(store, client_ref, pid))
    assert report["checks"]["largest_position_concentration"]["status"] == status
    assert bool(report["findings"]) == (status == "triggered")


def test_all_portfolios_keep_baseline_findings(store):
    from collections import Counter
    reports = [analyze_portfolio(d) for d in build_all(store, analysis_date=DAY)]
    counts = Counter(r["checks"]["largest_position_concentration"]["status"] for r in reports)
    assert len(reports) == 57
    assert counts == {"triggered": 40, "not_triggered": 8, "unavailable": 9}
    ids = [f["id"] for r in reports for f in r["findings"]]
    assert len(ids) == len(set(ids)) == 40
    json.dumps(reports, allow_nan=False)


def test_holly_payload_is_typed_and_actions_link_to_findings(store):
    payload = build_briefing_payload(dossier_for(store, "CASE-016", 315))
    assert len(payload.selected_findings) == 1
    finding = payload.selected_findings[0]
    assert isinstance(finding, Finding)
    assert finding.facts["weight_pct"] == pytest.approx(58.6)
    assert finding.calculation["position_value"] == 213200
    assert finding.calculation["portfolio_value"] == 363824.87
    assert finding.calculation["currency"] == "CHF"
    assert payload.action_candidates[0].finding_ids == [finding.id]
    assert payload.action_candidates[0].verb == "review"
    assert "suggested_action" not in to_jsonable(finding)
    assert any(g.check == "suitability" and "not_implemented" in g.status.value for g in payload.data_gaps)
    assert json.loads(json.dumps(to_jsonable(payload), allow_nan=False))["schema_version"] == "1.1"


def test_frodo_keeps_mismatch_and_gap(store):
    report = analyze_portfolio(dossier_for(store, "CASE-023"))
    assert report["reconciliation"]["difference"] == -500
    assert report["reconciliation"]["status"] == "mismatch"
    assert any(g["check"] == "largest_position_concentration" for g in report["briefing_payload"]["data_gaps"])


@pytest.mark.parametrize("value", [None, "Infinity", "-Infinity", "1e999", float("inf"), float("nan"), True])
def test_invalid_aum_never_enables_concentration(raw_data, value):
    store, d = mutated_holly(raw_data, lambda p: p.update(AssetsUnderManagementInDefaultCurrency=value))
    assert d.portfolio.aum_in_default_currency is None
    assert not d.check("concentration").ok
    result = analyze_portfolio(d)
    assert result["checks"]["largest_position_concentration"]["status"] == "unavailable"
    assert not result["findings"]
    json.dumps(result, allow_nan=False)
    if isinstance(value, str):
        assert "non_finite_number" in {e.code for e in store.errors()}


def test_absent_aum_never_enables_concentration(raw_data):
    _, d = mutated_holly(raw_data, lambda p: p.pop("AssetsUnderManagementInDefaultCurrency"))
    assert not d.check("concentration").ok
    assert reconcile_portfolio(d)["status"] == "incomplete"


def test_mixed_invalid_list_keeps_evidence_but_blocks_analysis(raw_data):
    store, d = mutated_holly(raw_data, lambda p: p["SecurityPositions"].append("bad row"))
    assert d.security_positions.presence is Presence.INVALID
    assert len(d.security_positions) == 1
    assert any(e.code == "invalid_list_item" for e in d.diagnostics)
    assert store.errors()
    assert not d.check("concentration").ok
    assert analyze_portfolio(d)["findings"] == []


def test_missing_position_value_blocks_analysis(raw_data):
    _, d = mutated_holly(raw_data, lambda p: p["SecurityPositions"][0].pop("TotalAmountInPortfolioCurrency"))
    assert not d.check("concentration").ok
    assert reconcile_portfolio(d)["status"] == "incomplete"


@pytest.mark.parametrize("value,presence", [(None, "null"), ("N/A", "invalid"), ([], "empty")])
def test_json_keeps_collection_availability(raw_data, value, presence):
    _, d = mutated_holly(raw_data, lambda p: p.update(SecurityPositions=value))
    encoded = to_jsonable(d)
    assert encoded["security_positions"]["presence"] == presence
    assert encoded["security_positions"]["items"] == []


def test_mixed_currencies_require_conversion(raw_data):
    _, d = mutated_holly(raw_data, lambda p: p.update(PortfolioCurrency="EUR"))
    report = analyze_portfolio(d)
    assert report["reconciliation"]["status"] == "incomplete"
    assert not report["findings"]
    assert any("FX" in x for x in report["reconciliation"]["missing_inputs"])


@pytest.mark.parametrize("security_value,triggered", [(99, False), (100, False), (101, True)])
def test_ten_percent_boundary(raw_data, security_value, triggered):
    def change(p):
        p["AssetsUnderManagementInDefaultCurrency"] = 1000
        p["SecurityPositions"][0]["TotalAmountInPortfolioCurrency"] = security_value
        p["AccountPositions"] = [{
            "AccountName": "Test cash", "Currency": "CHF",
            "TotalAmountInPortfolioCurrency": 1000 - security_value,
        }]
    _, d = mutated_holly(raw_data, change)
    report = analyze_portfolio(d)
    assert report["metrics"]["concentration"]["largest_position_above_threshold"] == triggered


def test_explicit_empty_positions_is_not_applicable(raw_data):
    def change(p):
        p["SecurityPositions"] = []
        p["AccountPositions"] = [{"AccountName": "Cash", "Currency": "CHF", "TotalAmountInPortfolioCurrency": 1000}]
        p["AssetsUnderManagementInDefaultCurrency"] = 1000
    _, d = mutated_holly(raw_data, change)
    assert analyze_portfolio(d)["checks"]["largest_position_concentration"]["status"] == "not_applicable"


def test_duplicate_security_ids_do_not_choose_wrong_evidence(raw_data):
    def change(p):
        first = p["SecurityPositions"][0]
        p["SecurityPositions"].append(copy.deepcopy(first))
        p["AssetsUnderManagementInDefaultCurrency"] += first["TotalAmountInPortfolioCurrency"]
    _, d = mutated_holly(raw_data, change)
    assert analyze_portfolio(d)["checks"]["largest_position_concentration"]["status"] == "unavailable"


def test_finding_requires_evidence():
    with pytest.raises(ValueError, match="evidence"):
        Finding(id="bad", section=Section.HEALTH, kind="risk", headline="Unsupported")


def test_reference_optional_and_output_deterministic(raw_data):
    store = load_store(raw_data)
    d = dossier_for(store, "CASE-016", 315)
    first = to_jsonable(build_briefing_payload(d))
    second = to_jsonable(build_briefing_payload(dossier_for(load_store(raw_data), "CASE-016", 315)))
    assert first == second
    assert first["selected_findings"][0]["facts"]["weight_pct"] == 58.6


def test_partial_batch_same_findings_and_evidence(raw_data):
    full = dossier_for(load_store(raw_data), "CASE-046")
    batch = dossier_for(load_store(raw_data[40:]), "CASE-046")
    assert to_jsonable(build_briefing_payload(full)) == to_jsonable(build_briefing_payload(batch))
    full = dossier_for(load_store(raw_data), "CASE-016", 315)
    batch = dossier_for(load_store([raw_data[15]]), "CASE-016", 315)
    assert to_jsonable(build_briefing_payload(full)) == to_jsonable(build_briefing_payload(batch))


def test_evidence_points_to_original_values(store, raw_data):
    import re
    by_ref = {c["ClientRef"]: c for c in raw_data}
    for d in build_all(store, analysis_date=DAY):
        for finding in build_briefing_payload(d).selected_findings:
            for evidence in finding.evidence:
                if evidence.source != "clients.json":
                    continue
                head, tail = evidence.path.split("]", 1)
                obj = by_ref[head.removeprefix("clients[")]
                for name, index in re.findall(r"([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]", tail):
                    obj = obj[name] if name else obj[int(index)]
                assert obj == evidence.value


def test_numeric_source_strings_remain_verbatim_in_evidence(raw_data):
    def change(p):
        p["SecurityPositions"][0]["TotalAmountInPortfolioCurrency"] = "213'200"
    _, d = mutated_holly(raw_data, change)
    finding = build_briefing_payload(d).selected_findings[0]
    assert finding.calculation["position_value"] == 213200
    assert any(e.value == "213'200" for e in finding.evidence)


def test_runner_uses_integrated_loader(tmp_path):
    output = tmp_path / "result.json"
    run = subprocess.run(
        [sys.executable, str(ROOT / "run_analysis.py"), "--analysis-date", DAY.isoformat(), "--output", str(output)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(output.read_text())
    assert payload["schema_version"] == "1.1"
    assert len(payload["portfolio_reports"]) == 57
    assert "briefing_payload" in payload["portfolio_reports"][0]
    assert "Findings generated: 40" in run.stdout
