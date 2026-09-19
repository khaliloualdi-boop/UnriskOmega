"""Tests for analytics.peers ranking logic.

Exercises compare_to_peers on a hand-built cohort (the pure part worth locking),
so the loader/performance/allocation stack isn't needed.
"""
import pytest

from analytics.peers import compare_to_peers


def rec(pid, strat, ret, vol=None, pid_metrics=None):
    m = {
        "annualized_return": ret,
        "annualized_volatility": vol,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "engine_volatility": None,
        "equity_weight": None,
        "cash_share": None,
    }
    if pid_metrics:
        m.update(pid_metrics)
    return {"portfolio_id": pid, "client_ref": f"C{pid}", "strategy_id": strat,
            "strategy_name": f"S{strat}", "risk_profile_id": 1, "metrics": m}


def test_percentile_and_group_stats():
    cohort = [
        rec(1, 5, 0.10),   # target
        rec(2, 5, 0.02),
        rec(3, 5, 0.06),
        rec(4, 5, 0.14),
        rec(9, 7, 0.99),   # different strategy -> excluded from group
    ]
    res = compare_to_peers(1, cohort, group_by="strategy_id")
    assert res["peer_group"]["group_value"] == 5
    assert res["peer_group"]["n_peers"] == 3          # excludes target and strat 7
    ar = res["comparison"]["annualized_return"]
    # peers = [0.02, 0.06, 0.14]; target 0.10 is above 2 of 3 -> 2/3
    assert ar["percentile_in_group"] == pytest.approx(2 / 3)
    assert ar["group"]["median"] == pytest.approx(0.06)
    assert ar["group"]["min"] == pytest.approx(0.02)
    assert ar["group"]["max"] == pytest.approx(0.14)


def test_market_distribution_uses_whole_cohort():
    cohort = [rec(i, i % 2, 0.01 * i) for i in range(1, 6)]
    res = compare_to_peers(1, cohort)
    dist = res["market"]["annualized_return"]
    assert dist["n"] == 5
    assert dist["min"] == pytest.approx(0.01)
    assert dist["max"] == pytest.approx(0.05)
    assert dist["median"] == pytest.approx(0.03)


def test_small_sample_flagged():
    cohort = [rec(1, 5, 0.10), rec(2, 5, 0.05)]  # only 1 peer
    res = compare_to_peers(1, cohort, min_peers=3)
    assert res["peer_group"]["n_peers"] == 1
    assert res["peer_group"]["small_sample"] is True


def test_missing_metric_yields_none_percentile():
    cohort = [rec(1, 5, None), rec(2, 5, 0.05), rec(3, 5, 0.06)]
    res = compare_to_peers(1, cohort)
    assert res["comparison"]["annualized_return"]["percentile_in_group"] is None


def test_target_not_found():
    cohort = [rec(2, 5, 0.05)]
    assert compare_to_peers(999, cohort)["status"] == "unavailable"


def _norec(pid, rp, equity):
    m = {k: None for k in ("annualized_return", "annualized_volatility", "sharpe_ratio",
                           "max_drawdown", "engine_volatility", "equity_weight", "cash_share")}
    m["equity_weight"] = equity
    return {"portfolio_id": pid, "client_ref": f"C{pid}", "strategy_id": 8,
            "strategy_name": "No strategy", "risk_profile_id": rp, "metrics": m}


def test_auto_no_strategy_falls_back_to_risk_profile():
    cohort = [_norec(1, 17, 0.5), _norec(2, 17, 0.6), _norec(3, 19, 0.4)]
    res = compare_to_peers(1, cohort, group_by="auto")
    assert res["peer_group"]["group_by"] == "risk_profile_id"
    assert res["peer_group"]["fallback_from"] == "strategy_id"
    assert res["peer_group"]["group_value"] == 17
    assert res["peer_group"]["n_peers"] == 1        # only pid 2 shares risk profile 17


def test_auto_no_strategy_no_risk_profile_falls_back_to_equity_band():
    cohort = [_norec(1, None, 0.55), _norec(2, None, 0.58), _norec(3, None, 0.90)]
    res = compare_to_peers(1, cohort, group_by="auto")
    assert res["peer_group"]["group_by"] == "equity_band"
    assert res["peer_group"]["group_value"] == "40-60%"
    assert res["peer_group"]["n_peers"] == 1        # pid 2 same band; pid 3 is 80-100%


def test_auto_real_strategy_uses_strategy():
    cohort = [rec(1, 5, 0.10), rec(2, 5, 0.05)]
    res = compare_to_peers(1, cohort, group_by="auto")
    assert res["peer_group"]["group_by"] == "strategy_id"
    assert res["peer_group"]["fallback_from"] is None


def test_group_by_risk_profile():
    cohort = [
        {"portfolio_id": 1, "client_ref": "C1", "strategy_id": 5, "strategy_name": "S5",
         "risk_profile_id": 17, "metrics": {"annualized_return": 0.10, **{k: None for k in
          ("annualized_volatility", "sharpe_ratio", "max_drawdown", "engine_volatility", "equity_weight", "cash_share")}}},
        {"portfolio_id": 2, "client_ref": "C2", "strategy_id": 6, "strategy_name": "S6",
         "risk_profile_id": 17, "metrics": {"annualized_return": 0.04, **{k: None for k in
          ("annualized_volatility", "sharpe_ratio", "max_drawdown", "engine_volatility", "equity_weight", "cash_share")}}},
    ]
    res = compare_to_peers(1, cohort, group_by="risk_profile_id")
    # grouped by risk profile 17 -> the strat-6 portfolio is now a peer
    assert res["peer_group"]["n_peers"] == 1
    assert res["comparison"]["annualized_return"]["percentile_in_group"] == pytest.approx(1.0)
