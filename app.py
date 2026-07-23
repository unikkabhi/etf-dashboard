"""
app.py — the Streamlit dashboard. Runs locally on your ThinkPad.
Start it with: streamlit run app.py
Requires etf_data.db to already exist (run pipeline.py at least once first).
"""

import streamlit as st
import sqlite3
import pandas as pd
import datetime

DB_PATH = "etf_data.db"

st.set_page_config(page_title="ETF filings dashboard", layout="wide")
st.title("ETF filings dashboard")


@st.cache_data(ttl=60)
def load_filings():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM filings", conn)
    conn.close()
    if not df.empty:
        df["filing_date"] = pd.to_datetime(df["filing_date"])
    return df


@st.cache_data(ttl=60)
def load_aum():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT * FROM aum_flows", conn)
        if not df.empty:
            df["period"] = pd.to_datetime(df["period"])
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


col1, col2 = st.columns([3, 1])
with col2:
    if st.button("Refresh data"):
        import pipeline
        today = datetime.date.today()
        week_ago = today - datetime.timedelta(days=7)
        pipeline.run_all(week_ago.isoformat(), today.isoformat())
        st.cache_data.clear()
        st.rerun()

filings_df = load_filings()
aum_df = load_aum()

if filings_df.empty:
    st.warning("No data yet — click Refresh data, or run `python pipeline.py` from the terminal first.")
    st.stop()

# ---- Filters (filings) ----
st.sidebar.header("Filters — Filings")
min_date = filings_df["filing_date"].min().date()
max_date = filings_df["filing_date"].max().date()
date_range = st.sidebar.date_input("Filings date range", value=(min_date, max_date))

fund_types = ["All"] + sorted(filings_df["fund_type"].dropna().unique().tolist())
selected_fund_type = st.sidebar.selectbox("Fund type", fund_types)

# Management style filter only meaningful (and only shown) when viewing ETFs
selected_style = "All"
if selected_fund_type == "ETF":
    styles = ["All"] + sorted(filings_df.loc[filings_df["fund_type"] == "ETF", "management_style"].dropna().unique().tolist())
    selected_style = st.sidebar.selectbox("Active / Passive", styles)

buckets = ["All"] + sorted(filings_df["bucket"].dropna().unique().tolist())
selected_bucket = st.sidebar.selectbox("Category", buckets)

# Industry filter only meaningful (and only shown) when viewing the thematic category
selected_industry = "All"
if selected_bucket == "thematic":
    industries = ["All"] + sorted(filings_df.loc[filings_df["bucket"] == "thematic", "industry"].dropna().unique().tolist())
    selected_industry = st.sidebar.selectbox("Industry", industries)

issuers = ["All"] + sorted(filings_df["filer_cik"].dropna().unique().tolist())
selected_issuer = st.sidebar.selectbox("Issuer (CIK)", issuers)

# Apply filters (filings)
filtered = filings_df.copy()
if len(date_range) == 2:
    start, end = date_range
    filtered = filtered[(filtered["filing_date"].dt.date >= start) & (filtered["filing_date"].dt.date <= end)]
if selected_fund_type != "All":
    filtered = filtered[filtered["fund_type"] == selected_fund_type]
if selected_style != "All":
    filtered = filtered[filtered["management_style"] == selected_style]
if selected_bucket != "All":
    filtered = filtered[filtered["bucket"] == selected_bucket]
if selected_industry != "All":
    filtered = filtered[filtered["industry"] == selected_industry]
if selected_issuer != "All":
    filtered = filtered[filtered["filer_cik"] == selected_issuer]

# ---- Chart 1: filings over time ----
st.subheader("New filings over time")
by_date = filtered.groupby(filtered["filing_date"].dt.date).size().reset_index(name="count")
st.bar_chart(by_date.set_index("filing_date"))

# ---- Chart 2: filings by category ----
st.subheader("Filings by category")
by_bucket = filtered.groupby("bucket").size().reset_index(name="count").sort_values("count", ascending=False)
st.bar_chart(by_bucket.set_index("bucket"))

# ---- Chart 2b: filings by industry (thematic only) ----
if selected_bucket == "thematic" and not filtered.empty:
    st.subheader("Thematic filings by industry")
    by_industry = filtered.groupby("industry").size().reset_index(name="count").sort_values("count", ascending=False)
    st.bar_chart(by_industry.set_index("industry"))

# ---- Chart 3: issuer leaderboard ----
st.subheader("Issuer launch leaderboard")

# Extract a clean issuer/trust name by stripping the "(CIK ...)" suffix from fund_name
filtered["issuer_name"] = filtered["fund_name"].str.replace(r"\s*\(CIK\s*\d+\)", "", regex=True).str.strip()

by_issuer = (
    filtered.groupby(["filer_cik", "issuer_name"])
    .size()
    .reset_index(name="filings")
    .sort_values("filings", ascending=False)
)
by_issuer = by_issuer[["issuer_name", "filings", "filer_cik"]]  # name first, CIK kept for reference
st.dataframe(by_issuer.head(20), use_container_width=True)
# ---- AUM flows (separate date range — N-PORT periods are quarterly, not recent) ----
st.subheader("AUM flows")
if aum_df.empty:
    st.info("No AUM data loaded yet — follow Day 3-4 in the Colab notebook to parse N-PORT data, "
            "then write it into the aum_flows table.")
else:
    aum_min_date = aum_df["period"].min().date()
    aum_max_date = aum_df["period"].max().date()
    st.sidebar.header("Filters — AUM")
    aum_date_range = st.sidebar.date_input(
        "AUM period range", value=(aum_min_date, aum_max_date),
        min_value=aum_min_date, max_value=aum_max_date,
    )

    aum_filtered = aum_df.copy()
    if len(aum_date_range) == 2:
        aum_start, aum_end = aum_date_range
        aum_filtered = aum_filtered[
            (aum_filtered["period"].dt.date >= aum_start) & (aum_filtered["period"].dt.date <= aum_end)
        ]
    if selected_bucket != "All" and "bucket" in aum_filtered.columns:
        aum_filtered = aum_filtered[aum_filtered["bucket"] == selected_bucket]

    if aum_filtered.empty:
        st.info("No AUM rows in the selected period range — try widening the AUM period range in the sidebar.")
    else:
        by_bucket_aum = aum_filtered.groupby("bucket")["aum_change"].sum().reset_index()
        st.bar_chart(by_bucket_aum.set_index("bucket"))

# ---- Thematic fund descriptions ----
if selected_bucket == "thematic" and not filtered.empty:
    st.subheader("Thematic fund descriptions")
    desc_view = filtered[["fund_name", "industry", "description"]].dropna(subset=["description"])
    if desc_view.empty:
        st.info("No descriptions available for the current thematic filings (best-effort extraction; not all filings yield one).")
    else:
        st.dataframe(desc_view, use_container_width=True)

# ---- Raw filings table ----
st.subheader("Raw filings")
display_cols = ["fund_name", "form_type", "filing_date", "filer_cik", "fund_type", "management_style", "bucket", "industry"]
st.dataframe(filtered[display_cols].sort_values("filing_date", ascending=False), use_container_width=True)
