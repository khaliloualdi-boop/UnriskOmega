"""Streamlit interface: analytics -> development -> news, in three sections.

Section 1 (Current situation) is pure deterministic display -- no AI.
Section 2 (Development) shows charts + an on-demand AI narration of the evolution.
Section 3 (News & advice) turns curated news into trends / opportunities / precautions.

The payload is computed once and cached; each AI call is lazy (only on click), so
a model or news hiccup never blanks the page.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

import charts
from ai_engine import (
    BriefingGenerationError,
    advise_from_news,
    describe_development,
)
from analytics.payload import build_briefing
from loader import load_store_from_files
from processing.dossier import is_consolidated

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Advisor AI Assistant", layout="wide")
st.title("Client Wealth Briefing Assistant")
st.caption("Portfolio analytics, development and curated news, for advisor review.")


@st.cache_resource
def get_store():
    return load_store_from_files(ROOT / "clients.json", ROOT / "reference.json")


@st.cache_data
def read_news(uploaded_bytes: bytes | None) -> dict | None:
    if not uploaded_bytes:
        return None
    result = json.loads(uploaded_bytes.decode("utf-8"))
    if not isinstance(result, dict):
        raise TypeError("The news file must contain one JSON object.")
    return result


@st.cache_data(show_spinner="Calculating the portfolio profile…")
def get_payload(portfolio_id: int, news_result: dict | None) -> dict:
    # cached per (portfolio, news) so switching tabs doesn't rerun the Monte Carlo
    return build_briefing(get_store(), portfolio_id, n_paths=2_000, news_result=news_result)


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
store = get_store()
clients = sorted(store.clients.values(), key=lambda item: item.client_ref or "")

st.sidebar.header("Briefing input")
client = st.sidebar.selectbox(
    "Client", clients, format_func=lambda item: f"{item.display_name} ({item.client_ref})")
portfolios = [item for item in client.portfolios if item.portfolio_id is not None]
portfolio = st.sidebar.selectbox(
    "Portfolio", portfolios,
    format_func=lambda item: (f"{item.portfolio_nr or item.portfolio_id} · "
                              f"{item.portfolio_currency or 'currency unknown'}"
                              + (" · consolidated" if is_consolidated(item) else "")))
news_upload = st.sidebar.file_uploader(
    "Curated news JSON (optional)", type=["json"],
    help="JSON produced by the portfolio news module for this client and portfolio.")

if is_consolidated(portfolio):
    st.warning("This is a consolidated view. Do not add its values to its component portfolios.")

try:
    news_result = read_news(news_upload.getvalue() if news_upload else None)
except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
    st.error(f"The news file could not be read: {exc}")
    st.stop()

if news_result:
    if (news_result.get("client_ref") not in (None, client.client_ref)
            or news_result.get("portfolio_id") not in (None, portfolio.portfolio_id)):
        st.error("The uploaded news file belongs to a different client or portfolio.")
        st.stop()

payload = get_payload(portfolio.portfolio_id, news_result)
if payload.get("status") == "unavailable":
    st.error(payload.get("reason", "This portfolio could not be analyzed."))
    st.stop()

profile = payload["profile"]
st.subheader(f"{client.display_name} · portfolio {profile['portfolio_number'] or profile['portfolio_id']}")
st.caption(f"Analysis date: {payload.get('analysis_date') or 'unknown'} · "
           f"News status: {payload['market_context'].get('status', 'attached')}")

tab_now, tab_dev, tab_news, tab_chat = st.tabs(
    ["Current situation", "Development", "News & advice", "Advisor Chat"]
)

# ============================ Section 1: current situation (no AI) ============================
with tab_now:
    c1, c2, c3, c4 = st.columns(4)
    aum = profile["aum"]
    c1.metric("AUM", f"{aum:,.0f} {profile['reporting_currency'] or ''}" if aum is not None else "—")
    c2.metric("Total return", fmt_pct(metric_value(payload, "performance", "total_return")))
    c3.metric("Annualized volatility", fmt_pct(metric_value(payload, "performance", "annualized_volatility")))
    c4.metric("Unavailable blocks", len(payload.get("data_quality", {}).get("unavailable", [])))

    holds = payload.get("top_holdings", {})
    if holds.get("status") == "calculated":
        st.markdown("**Top holdings**")
        st.dataframe([{
            "Name": h["name"], "ISIN": h["isin"], "Type": h["instrument_type"],
            "Value": h["value"], "Weight": fmt_pct(h["weight_of_total"]),
            "Asset class": h["asset_class"], "Sector": h["sector"], "Region": h["region"],
        } for h in holds["holdings"]], use_container_width=True, hide_index=True)
    else:
        st.info(f"Holdings unavailable: {holds.get('reason', 'no data')}")

    col_a, col_b = st.columns(2)
    accts = payload.get("accounts_by_currency", {})
    if accts.get("status") == "calculated":
        col_a.markdown("**Accounts (cash / crypto)**")
        col_a.dataframe([{"Currency": e["currency"], "Category": e["category"],
                          "Value": e["value"], "Weight": fmt_pct(e["weight_of_total"])}
                         for e in accts["by_currency"]], use_container_width=True, hide_index=True)
    drift = payload.get("mandate", {}).get("saa_drift", {})
    if drift.get("status") == "calculated":
        col_b.markdown("**Asset class vs SAA target**")
        col_b.dataframe([{"Category": r["category"], "Actual": fmt_pct(r["actual"]),
                          "Target": fmt_pct(r["target"]), "Status": r["status"]}
                         for r in drift["dimensions"].get("asset_class", [])],
                        use_container_width=True, hide_index=True)

# ============================ Section 2: development (charts + AI) ============================
with tab_dev:
    dates, values = nav_series(portfolio)
    ccy = profile["portfolio_currency"] or ""
    left, right = st.columns(2)
    left.plotly_chart(charts.nav_line(dates, values, currency=ccy), use_container_width=True)
    trough = (payload.get("performance", {}).get("metrics", {})
              .get("max_drawdown", {}).get("trough_date"))
    right.plotly_chart(charts.drawdown_curve(dates, values, trough_date=trough), use_container_width=True)

    lo, ro = st.columns(2)
    alloc = payload.get("allocation", {})
    weights = (alloc.get("allocation", {}).get("asset_class", {}).get("weights", {})
               if alloc.get("status") == "calculated" else {})
    lo.plotly_chart(charts.allocation_donut(weights), use_container_width=True)
    fan = payload.get("projection", {}).get("bootstrap", {}).get("fan_chart", [])
    ro.plotly_chart(charts.projection_fan(fan, currency=ccy), use_container_width=True)

    if st.button("Explain development with AI", type="primary"):
        with st.spinner("Writing the development commentary…"):
            try:
                st.markdown(describe_development(payload))
            except BriefingGenerationError as exc:
                st.error(str(exc))

# ============================ Section 3: news & advice (AI) ============================
with tab_news:
    mc = payload.get("market_context", {})
    if mc.get("status") == "to_be_provided_by_market_data_team":
        st.info("No curated news attached. Upload the news module's JSON in the sidebar to enable advice.")
    else:
        st.caption("Trends, opportunities and precautions are derived only from the attached news.")
        if st.button("Generate trends & advice", type="primary"):
            with st.spinner("Reading the curated news…"):
                try:
                    st.markdown(advise_from_news(payload))
                except BriefingGenerationError as exc:
                    st.error(str(exc))
                    

# ============================ Section 4: Chatbot ============================
with tab_chat:
    st.caption("Ask specific questions about holdings, performance, or notes for this client.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Handle user input
    if user_query := st.chat_input("Ask a question about this portfolio..."):
        st.session_state.messages.append({"role": "user", "content": user_query})
        with st.chat_message("user"):
            st.markdown(user_query)

        with st.chat_message("assistant"):
            with st.spinner("Searching payload..."):
                try:
                    from ai_engine import answer_chat_question
                    bot_response = answer_chat_question(payload, user_query)
                    st.markdown(bot_response)
                except BriefingGenerationError as exc:
                    bot_response = f"Error: {exc}"
                    st.error(bot_response)

        st.session_state.messages.append({"role": "assistant", "content": bot_response})