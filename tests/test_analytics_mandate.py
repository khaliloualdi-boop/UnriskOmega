"""Tests for analytics.mandate (SAA drift + guardrails)."""
import pytest

from analytics.mandate import analyze_guardrails, analyze_saa_drift


class _Ref:
    def __init__(self, saa, bands):
        self.strategic_allocations = saa
        self.saa_bands = bands


class _Portfolio:
    def __init__(self, saa_id=10, vol=None):
        self.strategic_asset_allocation_id = saa_id
        self.volatility = vol


def _alloc(asset_class):
    return {"status": "calculated", "allocation": {"asset_class": {"weights": asset_class}}}


# --------------------------------------------------------------------------
# SAA drift
# --------------------------------------------------------------------------
def _ref_with_bands():
    saa = {10: {"Name": "Balanced"}}
    bands = {
        (10, "AssetClass", "Shares"): {"MinPercentage": 0.2, "TargetPercentage": 0.5, "MaxPercentage": 0.6},
        (10, "AssetClass", "Bonds"): {"MinPercentage": 0.0, "TargetPercentage": 0.5, "MaxPercentage": 1.0},  # no real limits
    }
    return _Ref(saa, bands)


def test_drift_within_and_target():
    ref = _ref_with_bands()
    d = analyze_saa_drift(_Portfolio(), _alloc({"Shares": 0.55, "Bonds": 0.45}), ref)
    ac = {r["category"]: r for r in d["dimensions"]["asset_class"]}
    assert ac["Shares"]["status"] == "within"
    assert ac["Shares"]["drift"] == pytest.approx(0.05)
    # 0..1 band constrains nothing -> no_limits, never "within"
    assert ac["Bonds"]["status"] == "no_limits"
    assert d["breaches"] == []


def test_drift_above_max_is_a_breach():
    ref = _ref_with_bands()
    d = analyze_saa_drift(_Portfolio(), _alloc({"Shares": 0.75, "Bonds": 0.25}), ref)
    ac = {r["category"]: r for r in d["dimensions"]["asset_class"]}
    assert ac["Shares"]["status"] == "above_max"
    assert any(b["category"] == "Shares" and b["status"] == "above_max" for b in d["breaches"])


def test_drift_held_category_not_in_saa():
    ref = _ref_with_bands()
    d = analyze_saa_drift(_Portfolio(), _alloc({"Shares": 0.5, "Real estate": 0.5}), ref)
    ac = {r["category"]: r for r in d["dimensions"]["asset_class"]}
    assert ac["Real estate"]["status"] == "not_in_saa"


def test_drift_unavailable_when_saa_unresolved():
    ref = _Ref({}, {})
    d = analyze_saa_drift(_Portfolio(saa_id=999), _alloc({"Shares": 1.0}), ref)
    assert d["status"] == "unavailable"


# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------
def test_volatility_ceiling_and_equity_cap_within():
    port = _Portfolio(vol=0.09)
    g = analyze_guardrails(port, _alloc({"Shares": 0.6}),
                           {"MaxVola": 0.12, "EquityQuoteInPercent": 0.85},
                           {"VolatilityMinimum": 0.085, "VolatilityMaximum": 0.12})
    assert g["checks"]["volatility_vs_profile_max"]["status"] == "within"
    assert g["checks"]["equity_quote_vs_cap"]["status"] == "within"
    assert g["checks"]["volatility_vs_strategy_band"]["status"] == "within"
    assert g["breaches"] == []


def test_volatility_ceiling_breach():
    port = _Portfolio(vol=0.15)
    g = analyze_guardrails(port, _alloc({"Shares": 0.6}), {"MaxVola": 0.12})
    assert g["checks"]["volatility_vs_profile_max"]["status"] == "breach"


def test_equity_cap_breach():
    port = _Portfolio(vol=0.09)
    g = analyze_guardrails(port, _alloc({"Shares": 0.95}), {"EquityQuoteInPercent": 0.85})
    assert g["checks"]["equity_quote_vs_cap"]["status"] == "breach"


def test_strategy_band_below_and_above():
    below = analyze_guardrails(_Portfolio(vol=0.05), _alloc({"Shares": 0.4}), {},
                               {"VolatilityMinimum": 0.085, "VolatilityMaximum": 0.12})
    above = analyze_guardrails(_Portfolio(vol=0.20), _alloc({"Shares": 0.4}), {},
                               {"VolatilityMinimum": 0.085, "VolatilityMaximum": 0.12})
    assert below["checks"]["volatility_vs_strategy_band"]["status"] == "below_band"
    assert above["checks"]["volatility_vs_strategy_band"]["status"] == "above_band"


def test_guardrails_unavailable_when_inputs_missing():
    g = analyze_guardrails(_Portfolio(vol=None), _alloc({"Shares": 0.6}), None, None)
    assert g["checks"]["volatility_vs_profile_max"]["status"] == "unavailable"
    assert g["checks"]["equity_quote_vs_cap"]["status"] == "unavailable"
