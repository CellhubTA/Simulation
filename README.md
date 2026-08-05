# Campaign Simulation Tool

A Streamlit app that turns historical campaign delivery data into buying-rate
benchmarks and lets you simulate a new campaign in two directions:

- **Budget → Results**: enter a budget, get projected impressions, clicks,
  TrueView views, video completion funnel (25/50/75/100%), and unique users.
- **Target → Budget**: enter a target metric (e.g. "I want 500,000
  impressions"), get the required budget and the full projected delivery.
- **Multi-Channel Planner**: split a total budget across channel/bid-strategy
  segments and see combined projected results.

It also handles:
- **Currency conversion**: your data is in VND, this converts everything to
  JPY (or any target currency) using either a live FX rate or a manual rate
  you type in.
- **Bid strategy awareness**: benchmarks and projections are computed
  separately for **Target CPM** and **Target CPV** campaigns, since the
  buying mechanics (and which lever drives cost) differ between the two.

## ⚠️ Python version note

You asked about **Python 3.14**. As of writing (Aug 2026), Streamlit and
some of its dependencies are still catching up on 3.14 wheel support since
it's a very recent release. **I built and tested this against Python 3.12**,
which is fully supported by Streamlit/pandas/numpy today. I'd recommend
running the app on **3.11 or 3.12** for now. If you specifically need 3.14
(e.g. a language feature you're relying on elsewhere), let me know and I can
check current compatibility and adjust — but there's no functional reason
this app needs 3.14.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

## Using it

1. **Upload** your campaign report CSV in the sidebar (a `sample_data.csv`
   with your real 7-campaign export is included for testing).
2. **Set the FX rate**: pick "Enter manually" and type today's VND→JPY rate,
   or try "Fetch live rate" (requires the app to have internet access on
   whatever machine you run it on — it calls a free FX API).
3. Explore the **Historical Data & Benchmarks** tab to see cleaned data and
   derived rates (eCPM, CPC, CPV, CTR, view rate, VCR at each funnel stage,
   CPCV, unique-user rate) — all in JPY, per channel × bid strategy.
4. Use **Budget → Results** or **Target → Budget** for single-segment
   simulation, or **Multi-Channel Planner** to split a budget across
   segments.

## How the math works

- **Historical rates** are computed as *weighted averages* across all
  campaigns in a (Campaign type, Bid strategy type) segment — weighted by
  impressions/cost, not a simple mean of ratios, so bigger campaigns
  appropriately influence the benchmark more.
- **Target CPM campaigns**: impressions = budget ÷ eCPM × 1000. Everything
  else (clicks, views, completions, unique users) is derived by applying
  historical rates (CTR, view rate, VCR, unique-user rate) to that
  impression volume.
- **Target CPV campaigns**: TrueView views = budget ÷ CPV. Impressions are
  backed out using the historical view rate, then the same downstream rates
  apply as above.
- If a segment has **no historical data for a bid strategy** you select
  (e.g. your file currently only has Target CPM data, no Target CPV), the
  app falls back to CPM-derived estimates and shows a warning — it won't
  silently give you a wrong number without telling you.

## Extending it

- **More channels**: the "Campaign type" column in your export is used as
  the channel/segment dimension. Upload a file with multiple campaign types
  (Video, Display, Search, etc.) and each will get its own benchmark row
  automatically — no code changes needed.
- **More currencies**: change "Source currency" / "Target currency" in the
  sidebar; the live-fetch uses ISO currency codes (VND, JPY, USD, etc.).
- **Live FX in production**: `data_utils.fetch_live_fx_rate()` currently
  calls the free frankfurter.app API. Swap in your preferred FX provider
  there if you have one (e.g. a paid API with historical rates).

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI |
| `data_utils.py` | CSV cleaning, currency conversion, benchmark rate computation |
| `simulator.py` | Budget→Results and Target→Budget projection logic |
| `sample_data.csv` | Your uploaded historical data, for testing |
| `requirements.txt` | Python dependencies |
