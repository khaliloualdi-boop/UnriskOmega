"""Tests for analytics.holdings (top_holdings + accounts_by_currency)."""
import pytest

from analytics.holdings import accounts_by_currency, top_holdings


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
    def __init__(self, sid, amount, name="S", isin=None, currency="CHF"):
        self.security_id = sid
        self.total_amount_in_portfolio_currency = amount
        self.security_name = name
        self.isin = isin
        self.currency = currency
        self.portfolio_value_percentage = None


class _Acct:
    def __init__(self, amount, currency="CHF"):
        self.total_amount_in_portfolio_currency = amount
        self.currency = currency


class _Portfolio:
    def __init__(self, positions, accounts=(), aum=None):
        self.security_positions = positions
        self.account_positions = list(accounts)
        self.aum_in_default_currency = aum
        self.portfolio_id = 1
        self.portfolio_nr = "CASE-X-01"
        self.portfolio_currency = "CHF"
        self.factory_date_utc = "2026-09-01T00:00:00Z"


def _sec(ac="Shares", ind="Financials", cty="Switzerland", stype="Shares"):
    return {"SAA_AssetClassName": ac, "SAA_IndustryName": ind, "SAA_CountryGroupName": cty,
            "SAA_CurrencyGroupName": "Swiss francs", "SecurityTypeName": stype}


# --- top_holdings ---
def test_top_holdings_ranked_with_fields_and_weight():
    ref = _Ref({1: _sec(stype="Investment fund"), 2: _sec("Bonds", stype="Bonds")})
    port = _Portfolio(_List([_Pos(1, 300.0, "A", isin="CH1"), _Pos(2, 700.0, "B", isin="CH2")]), aum=1000.0)
    res = top_holdings(port, ref)
    assert res["status"] == "calculated"
    assert [h["name"] for h in res["holdings"]] == ["B", "A"]        # ranked by value
    assert res["holdings"][0]["weight_of_total"] == pytest.approx(0.7)
    assert res["holdings"][0]["instrument_type"] == "Bonds"
    assert res["holdings"][0]["isin"] == "CH2"
    assert res["weight_basis"] == "sum of all security and account positions"


def test_top_holdings_limit_reports_omitted():
    ref = _Ref({i: _sec() for i in range(1, 6)})
    port = _Portfolio(_List([_Pos(i, i * 100.0) for i in range(1, 6)]), aum=1500.0)
    res = top_holdings(port, ref, limit=2)
    assert res["shown"] == 2 and res["omitted"] == 3


def test_top_holdings_zero_limit_returns_none():
    ref = _Ref({1: _sec()})
    res = top_holdings(_Portfolio(_List([_Pos(1, 100.0)])), ref, limit=0)
    assert res["shown"] == 0
    assert res["omitted"] == 1
    assert res["holdings"] == []


def test_top_holdings_unavailable_without_reference():
    port = _Portfolio(_List([_Pos(1, 100.0)]))
    assert top_holdings(port, None)["status"] == "unavailable"


def test_top_holdings_weight_uses_position_total_when_no_aum():
    ref = _Ref({1: _sec()})
    port = _Portfolio(_List([_Pos(1, 500.0)]), aum=None)
    res = top_holdings(port, ref)
    assert res["weight_basis"] == "sum of all security and account positions"
    assert res["holdings"][0]["weight_of_total"] == pytest.approx(1.0)


# --- accounts_by_currency ---
def test_accounts_split_cash_and_crypto():
    port = _Portfolio(_List([_Pos(1, 900.0)]),
                      accounts=[_Acct(60.0, "CHF"), _Acct(40.0, "USD"), _Acct(100.0, "BTC")],
                      aum=1100.0)
    res = accounts_by_currency(port)
    by = {e["currency"]: e for e in res["by_currency"]}
    assert by["BTC"]["category"] == "crypto"
    assert by["CHF"]["category"] == "cash" and by["USD"]["category"] == "cash"
    assert res["totals"]["cash_ex_crypto"] == pytest.approx(100.0)
    assert res["totals"]["crypto"] == pytest.approx(100.0)
    assert res["totals"]["crypto_share"] == pytest.approx(100.0 / 1100.0)


def test_accounts_none_is_no_accounts():
    port = _Portfolio(_List([_Pos(1, 100.0)]), accounts=[])
    assert accounts_by_currency(port)["status"] == "no_accounts"


def test_invalid_account_amount_is_unavailable():
    port = _Portfolio(_List([_Pos(1, 100.0)]), accounts=[_Acct(None)])
    assert accounts_by_currency(port)["status"] == "unavailable"


def test_unknown_currency_is_not_mislabeled_as_crypto():
    port = _Portfolio(_List([_Pos(1, 100.0)]), accounts=[_Acct(10.0, "XYZ")])
    res = accounts_by_currency(port)
    assert res["by_currency"][0]["category"] == "unknown"
    assert res["totals"]["crypto"] == 0.0


def test_weights_use_positions_in_one_currency_not_default_currency_aum():
    port = _Portfolio(
        _List([_Pos(1, 900.0)]),
        accounts=[_Acct(100.0)],
        aum=5000.0,
    )
    res = accounts_by_currency(port)
    assert res["totals"]["cash_share_ex_crypto"] == pytest.approx(0.1)


def test_top_holdings_invalid_position_is_unavailable():
    ref = _Ref({1: _sec()})
    port = _Portfolio(_List([_Pos(1, None)]))
    assert top_holdings(port, ref)["status"] == "unavailable"
