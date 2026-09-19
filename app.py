"""One-click case analysis, live news and AI writing with persistent session results."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import charts
from presentation import narrative_markdown, news_status_message
from processing.dossier import is_consolidated
from workflow import case_files, load_case, run_workflow

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Advisor AI Assistant", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.block-container {max-width: 1440px; padding-top: 4.5rem;}
html, body, [data-testid="stApp"], [data-testid="stMarkdownContainer"],
input, textarea, button, h1, h2, h3, [data-testid="stMetric"] {
font-family: Arial, Helvetica, sans-serif;}
[data-testid="stMarkdownContainer"] p {line-height: 1.65;}
[data-testid="stMarkdownContainer"] strong {font-weight: 700; color: #252b36;}
h1 {font-size: 2rem !important; letter-spacing: -.04em;}
h2 {font-size: 1.45rem !important; letter-spacing: -.025em;}
h3 {font-size: 1.1rem !important;}
[data-testid="stMetric"] {background: white; border: 1px solid #e3e6eb;
border-radius: 12px; padding: 18px;}
[data-testid="stMetricValue"] {font-size: 1.8rem;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
[data-testid="stTabs"] button {font-weight: 600;}
@media(max-width: 700px) {
.block-container {padding: 4.5rem 1rem 1rem;}
[data-testid="stHorizontalBlock"] {flex-wrap: wrap;}
[data-testid="stColumn"] {min-width: 100% !important;}
}
@media print {[data-testid="stSidebar"],header,button {display:none !important;}}
</style>
""", unsafe_allow_html=True)
st.image(str(ROOT / "assets" / "uro-light.svg"), width=230)
st.title("Your portfolio, in perspective")
st.caption("A clear view of today. Context for the conversation ahead.")


def metric_value(payload: dict, block: str, metric: str):
    value = payload.get(block, {}).get("metrics", {}).get(metric, {})
    return value.get("value") if isinstance(value, dict) else None


def nav_series(portfolio):
    pts = [(hp.parsed_date, hp.value) for hp in getattr(portfolio, "performance_history", [])
           if getattr(hp, "parsed_date", None) is not None and getattr(hp, "value", None) is not None]
    pts.sort(key=lambda dv: dv[0])
    return [d for d, _ in pts], [v for _, v in pts]


def fmt_pct(x):
    return f"{x:.1%}" if isinstance(x, (int, float)) else "—"


# ----- inputs -----
st.sidebar.header("Briefing input")
case_path = st.sidebar.selectbox("Case file", case_files(ROOT), format_func=lambda p: p.name)
st.sidebar.caption("Add client JSON files to inputs, then refresh this page.")
if case_path is None:
    st.info("No case file selected.")
    st.stop()
try:
    store, fingerprint = load_case(case_path, ROOT / "reference.json")
except (OSError, ValueError, TypeError) as exc:
    st.error(f"Could not load the case: {exc}")
    st.stop()
clients = sorted(store.clients.values(), key=lambda item: item.client_ref or "")

client = st.sidebar.selectbox(
    "Client", clients, format_func=lambda item: f"{item.display_name} ({item.client_ref})")
if client is None:
    st.info("No usable client is available.")
    st.stop()
portfolios = [item for item in client.portfolios if item.portfolio_id is not None]
portfolio = st.sidebar.selectbox(
    "Portfolio", portfolios,
    format_func=lambda item: (f"{item.portfolio_nr or item.portfolio_id} · "
                              f"{item.portfolio_currency or 'currency unknown'}"
                              + (" · consolidated" if is_consolidated(item) else "")))
if portfolio is None:
    st.info("This client has no portfolio with a usable PortfolioId.")
    st.stop()

if is_consolidated(portfolio):
    st.warning("This is a consolidated view. Do not add its values to its component portfolios.")

selection = (str(case_path), fingerprint, client.client_ref, portfolio.portfolio_id)
if st.session_state.get("selection") != selection:
    st.session_state.pop("result", None)
    st.session_state["selection"] = selection
if st.button("Analyse and generate", type="primary"):
    st.session_state.pop("result", None)
    with st.status("Preparing your briefing…", expanded=True) as status:
        try:
            result = run_workflow(store, client.client_ref, portfolio.portfolio_id,
                                  progress=status.write,
                                  news_cache=st.session_state.setdefault("news_cache", {}),
                                  cache_key=selection)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
            status.update(label="Unable to complete this case", state="error")
        else:
            st.session_state["result"] = result
            limited = bool(result["errors"]) or result["payload"].get("market_context", {}).get(
                "status") in {"partial", "unavailable"}
            status.update(label="Ready with limitations" if limited else "Briefing ready",
                          state="complete")
result = st.session_state.get("result")
if result is None:
    st.info("Press Analyse and generate to calculate the profile, find news and write the briefing.")
    st.stop()
payload = result["payload"]
for section, message in result["errors"].items():
    st.warning(f"{section.capitalize()}: {message}")

profile = payload["profile"]
st.subheader(f"{client.display_name} · {profile['portfolio_number'] or profile['portfolio_id']}")
st.caption(f"Portfolio as of {payload.get('analysis_date') or 'unknown'} · "
           f"{profile.get('strategy', {}).get('name') or 'Strategy unavailable'}")
c1, c2, c3, c4 = st.columns(4)
aum = profile["aum"]
c1.metric("Portfolio value", f"{aum:,.0f} {profile['reporting_currency'] or ''}"
          if aum is not None else "Unavailable")
c2.metric("Period value change", fmt_pct(metric_value(payload, "performance", "total_return")))
c3.metric("Annualised volatility", fmt_pct(metric_value(payload, "performance", "annualized_volatility")))
cash = payload.get("accounts_by_currency", {}).get("totals", {}).get("cash_share_ex_crypto")
c4.metric("Cash allocation", fmt_pct(cash))
series = payload.get("performance", {}).get("series", {})
st.caption(f"History: {series.get('start_date') or 'unknown'} to {series.get('end_date') or 'unknown'}. "
           "Value changes include deposits and withdrawals; they are not flow-adjusted returns.")
gaps = payload.get("data_quality", {}).get("unavailable", [])
if gaps:
    st.warning(f"{len(gaps)} analysis areas have limited data. Review limitations before drawing conclusions.")

with st.container(border=True):
    st.caption("THE 60-SECOND BRIEFING")
    if result["texts"].get("briefing"):
        st.markdown(narrative_markdown(result["texts"]["briefing"]))
    else:
        st.info("The narrative is unavailable. You can still explore the analysis below.")

tab_now, tab_dev, tab_news = st.tabs(["Portfolio health", "Performance & outlook", "News & perspective"])
with tab_now:
    left, right = st.columns([1, 1.2], gap="large")
    alloc = payload.get("allocation", {})
    weights = (alloc.get("allocation", {}).get("asset_class", {}).get("weights", {})
               if alloc.get("status") == "calculated" else {})
    with left, st.container(border=True):
        st.subheader("Where your portfolio is invested")
        st.plotly_chart(charts.allocation_donut(weights), use_container_width=True)
    with right, st.container(border=True):
        st.subheader("Largest holdings")
        holds = payload.get("top_holdings", {})
        if holds.get("status") == "calculated":
            st.dataframe([{"Holding": h["name"], "Value": h["value"],
                           "Weight": fmt_pct(h["weight_of_total"]),
                           "Asset class": h["asset_class"]}
                          for h in holds["holdings"]], use_container_width=True, hide_index=True)
            st.caption("Weights use the holdings total defined by the analysis, not a guessed denominator.")
        else:
            st.info(holds.get("reason", "Holdings data unavailable."))
    with st.expander("Investment limits and account detail"):
        drift = payload.get("mandate", {}).get("saa_drift", {})
        if drift.get("status") == "calculated":
            st.dataframe([{"Category": r["category"], "Actual": fmt_pct(r["actual"]),
                           "Target": fmt_pct(r["target"]), "Status": r["status"]}
                          for r in drift["dimensions"].get("asset_class", [])],
                         use_container_width=True, hide_index=True)
        else:
            st.info(drift.get("reason", "Allocation limits unavailable."))
        accts = payload.get("accounts_by_currency", {})
        if accts.get("status") == "calculated":
            st.dataframe(accts["by_currency"], hide_index=True, use_container_width=True)

with tab_dev:
    dates, values = nav_series(portfolio)
    ccy = profile["portfolio_currency"] or ""
    with st.container(border=True):
        graph, narrative = st.columns([1.65, 1], gap="large")
        graph.plotly_chart(charts.nav_line(dates, values, currency=ccy), use_container_width=True)
        with narrative:
            st.subheader("The story behind the figures")
            st.markdown(narrative_markdown(result["texts"].get("development")
                                          or "Development commentary is unavailable."))
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.plotly_chart(charts.drawdown_curve(dates, values), use_container_width=True)
        st.caption("Declines from a previous portfolio-value peak. Cash flows may contribute to the movement.")
    with right, st.container(border=True):
        projection = payload.get("projection", {}).get("bootstrap", {})
        st.plotly_chart(charts.projection_fan(projection.get("fan_chart", []), currency=ccy),
                        use_container_width=True)
        st.caption("Illustrative scenarios, not a forecast or a guaranteed range of outcomes.")
        with st.expander("Scenario assumptions"):
            st.write(projection.get("assumptions") or projection.get("reason") or "Unavailable.")

with tab_news:
    mc = payload.get("market_context", {})
    if mc.get("status") in {"partial", "unavailable"}:
        st.warning(news_status_message(mc))
    articles = mc.get("articles", [])
    st.caption(f"News checked: {mc.get('news_as_of') or 'unavailable'}")
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        for article in articles:
            with st.container(border=True):
                st.caption(f"{article['source']} · {article['published_at']}")
                st.subheader(article["title"])
                st.write(article.get("snippet", ""))
                matches = list(dict.fromkeys(m.get("term", "") for m in article.get("matches", [])))
                if matches:
                    st.caption("Related exposure: " + ", ".join(matches))
                st.link_button("Read original source ↗", article["url"])
        if not articles:
            st.info(news_status_message(mc))
    with right, st.container(border=True):
        st.subheader("For your next conversation")
        st.markdown(narrative_markdown(result["texts"].get("news")
                                      or news_status_message(mc)))

with st.expander("Sources and data limitations"):
    for warning in payload.get("market_context", {}).get("warnings", []):
        st.warning(warning)
    for gap in gaps:
        st.write(f"{gap.get('block', 'Analysis')}: {gap.get('reason', 'Unavailable')}")
    for note in payload.get("data_quality", {}).get("notes", []):
        st.caption(note)
    st.json(payload, expanded=False)
st.caption("UnRiskOmega · Advisor briefing · Illustrative; not investment advice.")
