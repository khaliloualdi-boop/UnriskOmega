"""Tests for analytics.lookthrough (fund decomposition)."""
import pytest

from analytics.lookthrough import analyze_lookthrough


class _Ref:
    def __init__(self, securities, lookthrough):
        self._s = securities
        self.lookthrough = lookthrough

    def security(self, sid):
        return self._s.get(sid)


class _List(list):
    unavailable = False


class _Unavailable(list):
    unavailable = True


class _Pos:
    def __init__(self, sid, amount):
        self.security_id = sid
        self.total_amount_in_portfolio_currency = amount


class _Portfolio:
    def __init__(self, positions):
        self.security_positions = positions


def _row(industry=None, region=None, currency=None, weight=0.0):
    return {"IndustryName": industry, "CountryGroupName": region,
            "CurrencyGroupName": currency, "Weight": weight}


def test_fund_split_by_industry():
    # one fund, industry breakdown 60/40 (weights in percentage points)
    ref = _Ref(
        securities={1: {"SecurityTypeName": "Investment fund"}},
        lookthrough={1: [_row(industry="Financials", weight=60.0),
                         _row(industry="Health Care", weight=40.0)]},
    )
    res = analyze_lookthrough(_Portfolio(_List([_Pos(1, 1000.0)])), ref)
    ind = {d["category"]: d["weight"] for d in res["dimensions"]["industry"]}
    assert ind["Financials"] == pytest.approx(0.6)
    assert ind["Health Care"] == pytest.approx(0.4)
    assert res["coverage"]["fund_value_share"] == pytest.approx(1.0)
    assert res["coverage"]["funds_covered"] == 1


def test_non_fund_uses_own_saa_bucket():
    ref = _Ref(securities={2: {"SAA_IndustryName": "Utilities"}}, lookthrough={})
    res = analyze_lookthrough(_Portfolio(_List([_Pos(2, 500.0)])), ref)
    ind = {d["category"]: d["weight"] for d in res["dimensions"]["industry"]}
    assert ind["Utilities"] == pytest.approx(1.0)
    assert res["coverage"]["fund_value_share"] == pytest.approx(0.0)


def test_fund_without_composition_is_flagged_not_zeroed():
    ref = _Ref(securities={3: {"SecurityTypeName": "Investment fund"}}, lookthrough={})
    res = analyze_lookthrough(_Portfolio(_List([_Pos(3, 500.0)])), ref)
    assert res["coverage"]["funds_without_composition"] == 1
    # its exposure isn't dropped -> lands in Unclassified, not lost
    ind = {d["category"]: d["weight"] for d in res["dimensions"]["industry"]}
    assert ind.get("Unclassified") == pytest.approx(1.0)


def test_mixed_book_weights_sum_to_one():
    ref = _Ref(
        securities={1: {"SecurityTypeName": "Investment fund"}, 2: {"SAA_IndustryName": "Energy"}},
        lookthrough={1: [_row(industry="Financials", weight=100.0)]},
    )
    res = analyze_lookthrough(_Portfolio(_List([_Pos(1, 600.0), _Pos(2, 400.0)])), ref)
    total = sum(d["weight"] for d in res["dimensions"]["industry"])
    assert total == pytest.approx(1.0)
    assert res["coverage"]["fund_value_share"] == pytest.approx(0.6)


def test_unavailable_without_reference():
    assert analyze_lookthrough(_Portfolio(_List([_Pos(1, 100.0)])), None)["status"] == "unavailable"


def test_unavailable_positions():
    ref = _Ref({}, {})
    assert analyze_lookthrough(_Portfolio(_Unavailable()), ref)["status"] == "unavailable"


def test_invalid_position_amount_is_unavailable():
    ref = _Ref({1: {"SecurityTypeName": "Investment fund"}}, {})
    assert analyze_lookthrough(_Portfolio(_List([_Pos(1, None)])), ref)["status"] == "unavailable"


def test_short_position_is_unavailable():
    ref = _Ref({1: {"SecurityTypeName": "Investment fund"}}, {})
    assert analyze_lookthrough(_Portfolio(_List([_Pos(1, -100.0)])), ref)["status"] == "unavailable"
