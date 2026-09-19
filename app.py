import streamlit as st
from loader import load_store_from_files
from pipeline import build_briefing_payload
from ai_engine import generate_wealth_briefing

st.set_page_config(page_title="Advisor AI Assistant", layout="wide")
st.title("Client Wealth Briefing Assistant")

# 1. Load Data
@st.cache_data
def get_data():
    return load_store_from_files("clients.json", "reference.json")

clients_data, reference_data = get_data()

# 2. Sidebar Client Selection
st.sidebar.header("Select Client")
client_names = [f"{c['name']} (ID: {c['id']})" for c in clients_data]
selected_client_str = st.sidebar.selectbox("Choose a client profile", client_names)

if selected_client_str:
    client_id = selected_client_str.split("ID: ")[1].rstrip(")")
    
    # 3. Build Briefing Payload
    payload = build_briefing_payload(client_id, clients_data, reference_data)
    
    st.subheader(f"Briefing Dashboard: {payload.client_name}")
    
    # Render Pre-calculated Metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Cash Ratio", f"{payload.cash_ratio:.1%}")
    col2.metric("Rule Violations", len(payload.violations))
    col3.metric("Data Completeness", payload.data_gap_status)
    
    st.divider()
    
    # 4. Generate AI Briefing Button
    if st.button("Generate AI 60-Second Briefing"):
        with st.spinner("Analyzing portfolio, rule findings, and news..."):
            briefing_text = generate_wealth_briefing(payload)
            st.markdown("### AI Executive Summary")
            st.info(briefing_text)