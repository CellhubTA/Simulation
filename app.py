"""
Campaign Simulation Tool
-------------------------
Upload historical campaign delivery data (Google Ads / DV360-style export),
convert costs from VND to JPY, derive historical benchmark rates per
channel + bid strategy, and simulate a new campaign either from a budget
or from a target metric.

Run with:
    streamlit run app.py
"""

import numpy as np
import pandas as pd
import streamlit as st

import data_utils as du
import simulator as sim

st.set_page_config(page_title="Campaign Simulation Tool", layout="wide")


# --------------------------------------------------------------------------
# Password gate
# --------------------------------------------------------------------------
def check_password() -> bool:
    """
    Simple password gate using st.secrets. Add a secret named
    `app_password` (see README for how to set this locally and on
    Streamlit Community Cloud). If no secret is configured, the app
    warns and runs unprotected rather than locking everyone out.
    """
    configured_password = st.secrets.get("app_password", None)
    if not configured_password:
        st.warning(
            "⚠️ No password is configured for this app (missing `app_password` in secrets). "
            "Running without a password gate -- see README to set one up.",
            icon="⚠️",
        )
        return True

    def _on_submit():
        if st.session_state.get("password_input") == configured_password:
            st.session_state["password_ok"] = True
        else:
            st.session_state["password_ok"] = False

    if st.session_state.get("password_ok"):
        return True

    st.title("🔒 Campaign Simulation Tool")
    st.text_input("Password", type="password", key="password_input", on_change=_on_submit)
    if st.session_state.get("password_ok") is False:
        st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()

# --------------------------------------------------------------------------
# Sidebar: data upload + currency settings
# --------------------------------------------------------------------------

st.sidebar.header("1. Upload historical data")
uploaded_file = st.sidebar.file_uploader(
    "Campaign report CSV", type=["csv"], help="Google Ads / DV360-style campaign export"
)

st.sidebar.header("2. Currency conversion")
source_currency = st.sidebar.text_input("Source currency (in the file)", value="VND")
target_currency = st.sidebar.text_input("Target currency", value="JPY")

fx_mode = st.sidebar.radio("FX rate source", ["Fetch live rate", "Enter manually"], index=1)

if fx_mode == "Fetch live rate":
    if st.sidebar.button("Fetch live rate now"):
        rate, source_label = du.fetch_live_fx_rate(source_currency, target_currency)
        if rate is not None:
            st.session_state["fx_rate"] = rate
            st.session_state["fx_source"] = source_label
        else:
            st.sidebar.warning(f"Live fetch failed: {source_label}. Falling back to manual entry.")
    fx_rate = st.session_state.get("fx_rate", du.FALLBACK_VND_TO_JPY)
    fx_source = st.session_state.get("fx_source", "not yet fetched (using fallback constant)")
    st.sidebar.caption(f"Current rate: 1 {source_currency} = {fx_rate:.6f} {target_currency}  ·  {fx_source}")
else:
    fx_rate = st.sidebar.number_input(
        f"1 {source_currency} = ? {target_currency}",
        min_value=0.0,
        value=float(st.session_state.get("fx_rate", du.FALLBACK_VND_TO_JPY)),
        format="%.6f",
    )
    st.session_state["fx_rate"] = fx_rate
    fx_source = "manual override"

st.sidebar.caption(
    "Note: this environment may not have live internet access when fetching rates. "
    "If 'Fetch live rate' fails, switch to manual entry -- e.g. check "
    "google.com/finance or xe.com and type the current rate in."
)

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

st.title("📊 Campaign Simulation Tool")
st.caption(
    "Estimate buying rates and projected delivery metrics for a new campaign, "
    "based on your historical channel performance."
)

if uploaded_file is None:
    st.info("👈 Upload a historical campaign report CSV in the sidebar to get started.")
    st.markdown(
        """
        **Expected format:** a Google Ads / DV360-style campaign report export with columns such as
        `Campaign`, `Currency code`, `Campaign type`, `Cost`, `Bid strategy type`, `Impr.`,
        `TrueView views`, `Avg. CPM`, `Clicks`, `CTR`, `Video played to 25/50/75/100%`, `Unique users`.
        """
    )
    st.stop()

try:
    raw_df = du.load_campaign_csv(uploaded_file)
except Exception as e:
    st.error(f"Couldn't read this file: {e}")
    st.stop()

# --------------------------------------------------------------------------
# Audience tagging
# --------------------------------------------------------------------------
st.subheader("Tag each campaign with a target audience")
st.caption(
    "Your file doesn't include an audience column, so tag campaigns here. "
    "Benchmarks and simulations will then be split by audience too. "
    "Leave as 'All' if a campaign wasn't audience-targeted."
)

if "audience_tags" not in st.session_state:
    st.session_state["audience_tags"] = raw_df[["Campaign"]].copy()
    st.session_state["audience_tags"]["Audience"] = raw_df["Audience"]

edited_tags = st.data_editor(
    st.session_state["audience_tags"],
    column_config={
        "Audience": st.column_config.SelectboxColumn(
            "Audience", options=du.AUDIENCE_OPTIONS, required=True
        ),
        "Campaign": st.column_config.TextColumn("Campaign", disabled=True),
    },
    hide_index=True,
    use_container_width=True,
    key="audience_editor",
)
st.session_state["audience_tags"] = edited_tags

raw_df = raw_df.merge(
    edited_tags.rename(columns={"Audience": "Audience_tagged"}), on="Campaign", how="left"
)
raw_df["Audience"] = raw_df["Audience_tagged"].fillna(raw_df["Audience"])
raw_df = raw_df.drop(columns=["Audience_tagged"])

df_jpy = du.add_jpy_columns(raw_df, fx_rate, cost_currency=source_currency)
benchmarks = du.compute_benchmarks(df_jpy)

if benchmarks.empty:
    st.error("No usable rows found after cleaning -- check the uploaded file.")
    st.stop()

benchmarks["segment_label"] = (
    benchmarks["segment"] + " · " + benchmarks["bid_strategy"] + " · " + benchmarks["audience"]
)

tab_data, tab_budget, tab_target, tab_planner = st.tabs(
    ["📁 Historical Data & Benchmarks", "💰 Budget → Results", "🎯 Target → Budget", "🧮 Multi-Channel Planner"]
)

# --------------------------------------------------------------------------
# TAB: Historical Data & Benchmarks
# --------------------------------------------------------------------------
with tab_data:
    st.subheader("Cleaned historical data")
    st.dataframe(raw_df, use_container_width=True)

    st.subheader(f"Cost converted to {target_currency}")
    show_cols = [c for c in ["Campaign", "Campaign type", "Bid strategy type", "Cost", "Cost (JPY)"] if c in df_jpy.columns]
    st.dataframe(df_jpy[show_cols], use_container_width=True)

    st.subheader("Historical benchmark rates by channel & bid strategy")
    st.caption("Weighted by impressions/cost across all campaigns in each segment.")
    display_bench = benchmarks.drop(columns=["segment_label"]).copy()
    for c in ["ctr", "view_rate", "vcr_25", "vcr_50", "vcr_75", "vcr_100", "uu_rate"]:
        display_bench[c] = (display_bench[c] * 100).round(3).astype(str) + "%"
    for c in ["total_cost_jpy", "ecpm_jpy", "cpc_jpy", "cpv_jpy", "cpcv_jpy"]:
        display_bench[c] = display_bench[c].round(2)
    st.dataframe(display_bench, use_container_width=True)

# --------------------------------------------------------------------------
# TAB: Budget -> Results
# --------------------------------------------------------------------------
with tab_budget:
    st.subheader("Project results from a budget")
    col1, col2 = st.columns(2)
    with col1:
        seg_label = st.selectbox("Channel / bid strategy segment", benchmarks["segment_label"], key="budget_seg")
        budget_input = st.number_input(f"Budget ({target_currency})", min_value=0.0, value=50000.0, step=1000.0)
    with col2:
        row = benchmarks.loc[benchmarks["segment_label"] == seg_label].iloc[0]
        st.metric("Historical eCPM", f"¥{row['ecpm_jpy']:.2f}" if pd.notna(row["ecpm_jpy"]) else "n/a")
        st.metric("Historical CPV", f"¥{row['cpv_jpy']:.2f}" if pd.notna(row["cpv_jpy"]) else "n/a")
        st.metric("Historical CTR", f"{row['ctr']*100:.3f}%" if pd.notna(row["ctr"]) else "n/a")

    result = sim.simulate_from_budget(row.to_dict(), budget_input)

    for w in result.warnings:
        st.warning(w)

    st.markdown("### Projected delivery")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Impressions", f"{result.impressions:,.0f}" if pd.notna(result.impressions) else "n/a")
    m2.metric("Clicks", f"{result.clicks:,.0f}" if pd.notna(result.clicks) else "n/a")
    m3.metric("TrueView views", f"{result.trueview_views:,.0f}" if pd.notna(result.trueview_views) else "n/a")
    m4.metric("Unique users", f"{result.unique_users:,.0f}" if pd.notna(result.unique_users) else "n/a")

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Video 25%", f"{result.video_25:,.0f}" if pd.notna(result.video_25) else "n/a")
    m6.metric("Video 50%", f"{result.video_50:,.0f}" if pd.notna(result.video_50) else "n/a")
    m7.metric("Video 75%", f"{result.video_75:,.0f}" if pd.notna(result.video_75) else "n/a")
    m8.metric("Video 100% (VCR)", f"{result.video_100:,.0f} ({result.vcr_100*100:.2f}%)" if pd.notna(result.video_100) else "n/a")

    st.markdown("### Implied buying rates at this budget")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("eCPM", f"¥{result.ecpm_jpy:,.2f}" if pd.notna(result.ecpm_jpy) else "n/a")
    r2.metric("CPC", f"¥{result.cpc_jpy:,.2f}" if pd.notna(result.cpc_jpy) else "n/a")
    r3.metric("CPV", f"¥{result.cpv_jpy:,.2f}" if pd.notna(result.cpv_jpy) else "n/a")
    r4.metric("CPCV", f"¥{result.cpcv_jpy:,.2f}" if pd.notna(result.cpcv_jpy) else "n/a")

    st.markdown("### How eCPM shifts as spend increases")
    if row.get("has_ecpm_curve"):
        st.caption(
            f"Fitted from {int(row['n_campaigns'])} historical campaigns in this segment "
            f"(R² = {row['ecpm_curve_r2']:.2f}). Curve: eCPM = {row['ecpm_curve_intercept']:.2f} "
            f"+ {row['ecpm_curve_slope']:.2f} × ln(budget)."
        )
    else:
        st.caption(
            f"Only {int(row['n_campaigns'])} historical campaign(s) in this segment -- "
            "need 3+ to reliably model how eCPM changes with spend, so this shows a flat rate."
        )
    budget_range = np.linspace(max(budget_input * 0.2, 1000), budget_input * 3, 30)
    curve_df = pd.DataFrame(
        {
            "Budget (JPY)": budget_range,
            "Projected eCPM (JPY)": [du.project_ecpm(row.to_dict(), b) for b in budget_range],
        }
    ).set_index("Budget (JPY)")
    st.line_chart(curve_df)

# --------------------------------------------------------------------------
# TAB: Target -> Budget
# --------------------------------------------------------------------------
with tab_target:
    st.subheader("Back into required budget from a target metric")
    col1, col2 = st.columns(2)
    with col1:
        seg_label_t = st.selectbox("Channel / bid strategy segment", benchmarks["segment_label"], key="target_seg")
        target_metric = st.selectbox("Target metric", sim.TARGETABLE_METRICS)
    with col2:
        target_value = st.number_input(f"Target value ({target_metric})", min_value=0.0, value=500000.0, step=1000.0)

    row_t = benchmarks.loc[benchmarks["segment_label"] == seg_label_t].iloc[0]
    result_t = sim.simulate_from_target(row_t.to_dict(), target_metric, target_value)

    for w in result_t.warnings:
        st.warning(w)

    st.markdown("### Required budget")
    st.metric(f"Budget needed ({target_currency})", f"¥{result_t.budget_jpy:,.0f}" if pd.notna(result_t.budget_jpy) else "n/a")

    st.markdown("### Full projected delivery at that budget")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Impressions", f"{result_t.impressions:,.0f}" if pd.notna(result_t.impressions) else "n/a")
    m2.metric("Clicks", f"{result_t.clicks:,.0f}" if pd.notna(result_t.clicks) else "n/a")
    m3.metric("TrueView views", f"{result_t.trueview_views:,.0f}" if pd.notna(result_t.trueview_views) else "n/a")
    m4.metric("Unique users", f"{result_t.unique_users:,.0f}" if pd.notna(result_t.unique_users) else "n/a")

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Video 25%", f"{result_t.video_25:,.0f}" if pd.notna(result_t.video_25) else "n/a")
    m6.metric("Video 50%", f"{result_t.video_50:,.0f}" if pd.notna(result_t.video_50) else "n/a")
    m7.metric("Video 75%", f"{result_t.video_75:,.0f}" if pd.notna(result_t.video_75) else "n/a")
    m8.metric("Video 100% (VCR)", f"{result_t.video_100:,.0f}" if pd.notna(result_t.video_100) else "n/a")

# --------------------------------------------------------------------------
# TAB: Multi-Channel Planner
# --------------------------------------------------------------------------
with tab_planner:
    st.subheader("Allocate a total budget across multiple channel/strategy segments")
    total_budget = st.number_input(f"Total budget ({target_currency})", min_value=0.0, value=200000.0, step=5000.0)

    st.caption("Set the % of total budget for each segment (should sum to 100%).")
    alloc_pcts = {}
    cols = st.columns(min(len(benchmarks), 4) or 1)
    for i, (_, r) in enumerate(benchmarks.iterrows()):
        with cols[i % len(cols)]:
            pct = st.slider(r["segment_label"], 0, 100, int(100 / len(benchmarks)), key=f"alloc_{i}")
            alloc_pcts[(r["segment"], r["bid_strategy"], r["audience"])] = pct / 100.0

    total_pct = sum(alloc_pcts.values()) * 100
    if abs(total_pct - 100) > 0.01:
        st.warning(f"Allocations sum to {total_pct:.0f}%, not 100%. Results will scale accordingly.")

    if st.button("Run planner"):
        results = sim.allocate_budget_across_segments(benchmarks, total_budget, alloc_pcts)
        if not results:
            st.info("No budget allocated to any segment.")
        else:
            res_df = pd.DataFrame(results)
            display_cols = [
                "segment", "bid_strategy", "budget_jpy", "impressions", "clicks",
                "trueview_views", "video_100", "unique_users", "ecpm_jpy", "cpc_jpy", "cpv_jpy",
            ]
            display_cols = [c for c in display_cols if c in res_df.columns]
            st.dataframe(res_df[display_cols].round(2), use_container_width=True)

            totals = res_df[["budget_jpy", "impressions", "clicks", "trueview_views", "video_100", "unique_users"]].sum()
            st.markdown("### Totals across all channels")
            t1, t2, t3, t4 = st.columns(4)
            t1.metric("Total Budget", f"¥{totals['budget_jpy']:,.0f}")
            t2.metric("Total Impressions", f"{totals['impressions']:,.0f}")
            t3.metric("Total Clicks", f"{totals['clicks']:,.0f}")
            t4.metric("Total Unique Users", f"{totals['unique_users']:,.0f}")
