"""Tests for analytics.allocation.

Uses lightweight fakes for the typed model so the maths is exercised without
the loader: a security master keyed by id, positions with an amount + id, and
cash accounts.
"""
import pytest

from analytics.allocation import UNCLASSIFIED, analyze_allocation


class _Ref:
    def __init__(self, securities):
        self._s = securities

    def security(self, sid):
        return self._s.get(sid)


class _List(list):
    unavailable = False


class _Unavailable(list):
    unavailable = True


class _Pos:
    def __init__(self, sid, amount, name="S"):
        self.security_id = sid
        self.total_amount_in_portfolio_currency = amount
        self.security_name = name
        self.portfolio_value_percentage = None


class _Acct:
    def __init__(self, amount, currency="CHF"):
        self.total_amount_in_portfolio_currency = amount
        self.currency = currency


class _Portfolio:
    def __init__(self, positions, accounts=(), aum=None, pid=1, nr="CASE-X-01"):
        self.security_positions = positions
        self.account_positions: _List | _Unavailable = _List(accounts)
        self.aum_in_default_currency = aum
        self.portfolio_id = pid
        self.portfolio_nr = nr
        self.portfolio_currency = "CHF"


def _sec(ac, cur="Swiss francs", cty="Switzerland", ind="Financials"):
    return {
        "SAA_AssetClassName": ac,
        "SAA_CurrencyGroupName": cur,
        "SAA_CountryGroupName": cty,
        "SAA_IndustryName": ind,
    }


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
def test_no_reference_unavailable():
    port = _Portfolio(_List([_Pos(1, 100.0)]))
    assert analyze_allocation(port, None)["status"] == "unavailable"


def test_unavailable_positions():
    port = _Portfolio(_Unavailable())
    assert analyze_allocation(port, _Ref({}))["status"] == "unavailable"


def test_unavailable_accounts():
    port = _Portfolio(_List([_Pos(1, 100.0)]))
    port.account_positions = _Unavailable()
    assert analyze_allocation(port, _Ref({1: _sec("Shares")}))["status"] == "unavailable"


def test_invalid_position_amount_is_unavailable():
    port = _Portfolio(_List([_Pos(1, None)]))
    assert analyze_allocation(port, _Ref({1: _sec("Shares")}))["status"] == "unavailable"


def test_empty_positions():
    port = _Portfolio(_List([]))
    assert analyze_allocation(port, _Ref({}))["status"] == "no_security_positions"


# --------------------------------------------------------------------------
# Core allocation
# --------------------------------------------------------------------------
def test_asset_class_includes_cash_as_liquidity_and_sums_to_one():
    ref = _Ref({1: _sec("Shares"), 2: _sec("Bonds")})
    port = _Portfolio(
        _List([_Pos(1, 600.0), _Pos(2, 300.0)]),
        accounts=[_Acct(100.0)],
        aum=1000.0,
    )
    res = analyze_allocation(port, ref)
    w = res["allocation"]["asset_class"]["weights"]
    assert w["Shares"] == pytest.approx(0.6)
    assert w["Bonds"] == pytest.approx(0.3)
    assert w["Liquidity"] == pytest.approx(0.1)   # cash folded in
    assert sum(w.values()) == pytest.approx(1.0)
    assert res["coverage"]["positions_vs_aum"] == pytest.approx(1.0)
    assert res["coverage"]["cash_share"] == pytest.approx(0.1)
    assert res["allocation"]["currency_group"]["weights"]["Swiss francs"] == pytest.approx(1.0)
    assert res["allocation"]["country_group"]["weights"]["Not classified"] == pytest.approx(0.1)
    assert res["allocation"]["industry"]["weights"]["Not classified"] == pytest.approx(0.1)
    for dimension in res["allocation"].values():
        assert sum(dimension["weights"].values()) == pytest.approx(1.0)


def test_unresolved_security_is_unclassified_not_dropped():
    ref = _Ref({1: _sec("Shares")})           # id 2 is unknown
    port = _Portfolio(_List([_Pos(1, 500.0), _Pos(2, 500.0)]))
    res = analyze_allocation(port, ref)
    w = res["allocation"]["asset_class"]["weights"]
    assert w[UNCLASSIFIED] == pytest.approx(0.5)
    assert res["allocation"]["currency_group"]["classified_share"] == pytest.approx(0.5)


def test_diversification_two_equal_holdings():
    ref = _Ref({1: _sec("Shares"), 2: _sec("Shares")})
    port = _Portfolio(_List([_Pos(1, 500.0), _Pos(2, 500.0)]))
    d = analyze_allocation(port, ref)["diversification"]
    assert d["hhi"] == pytest.approx(0.5)
    assert d["effective_holdings"] == pytest.approx(2.0)
    assert d["holdings"] == 2


def test_crypto_account_flagged():
    ref = _Ref({1: _sec("Shares")})
    port = _Portfolio(_List([_Pos(1, 900.0)]), accounts=[_Acct(100.0, "BTC")])
    res = analyze_allocation(port, ref)
    assert res["coverage"]["has_crypto_account"] is True
    # crypto cash still aggregates into Liquidity by its CHF amount...
    assert res["allocation"]["asset_class"]["weights"]["Liquidity"] == pytest.approx(0.1)
    # ...but its CHF value stays visible separately, not hidden in cash.
    assert res["coverage"]["crypto_value_chf"] == pytest.approx(100.0)
    assert res["coverage"]["crypto_share"] == pytest.approx(0.1)


def test_unknown_account_currency_is_not_crypto():
    ref = _Ref({1: _sec("Shares")})
    port = _Portfolio(_List([_Pos(1, 900.0)]), accounts=[_Acct(100.0, "XYZ")])
    res = analyze_allocation(port, ref)
    assert res["coverage"]["has_crypto_account"] is False
    assert res["coverage"]["crypto_value_chf"] == 0.0


def test_largest_holding_identified():
    ref = _Ref({1: _sec("Shares"), 2: _sec("Bonds")})
    port = _Portfolio(_List([_Pos(1, 700.0, "Big"), _Pos(2, 300.0, "Small")]))
    top = analyze_allocation(port, ref)["diversification"]["largest_holding"]
    assert top["security_name"] == "Big"
    assert top["weight_of_securities"] == pytest.approx(0.7)
