"""
data_utils.py
--------------
Loading, cleaning, currency conversion, and historical benchmark-rate
computation for the campaign simulation app.

Expects a Google Ads / DV360-style "Campaign report" export CSV, e.g.:

    Campaign report
    "July 27, 2026 - August 5, 2026"
    Campaign,Budget type,Currency code,Campaign type,Cost,Bid strategy type,
    Impr.,TrueView views,TrueView view rate (In-stream), ... ,Unique users
    Campaign1,Daily,VND,Video,2615475,Target CPM,"101,597",920, --, --, ...

The first two rows are a title/date header and are skipped automatically.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# --------------------------------------------------------------------------
# Column definitions
# --------------------------------------------------------------------------

# Columns that are plain integers but formatted with thousands separators,
# e.g. "101,597"
THOUSANDS_COLS = [
    "Impr.",
    "TrueView views",
    "Unique users",
]

# Columns already numeric in the raw file (no comma formatting), but we
# still coerce them defensively.
NUMERIC_COLS = [
    "Cost",
    "Avg. CPM",
    "TrueView avg. CPV",
    "Clicks",
]

# Columns expressed as percentages, e.g. "0.91%" or " --" for not-applicable.
PERCENT_COLS = [
    "TrueView view rate (In-stream)",
    "TrueView view rate (In-feed)",
    "TrueView view rate (Shorts)",
    "CTR",
    "Video played to 25%",
    "Video played to 50%",
    "Video played to 75%",
    "Video played to 100%",
]

REQUIRED_COLS = [
    "Campaign",
    "Currency code",
    "Campaign type",
    "Cost",
    "Bid strategy type",
    "Impr.",
]

BID_STRATEGIES = ["Target CPM", "Target CPV"]

# Preset audience options shown in the app's audience-tagging UI. Freeform
# text is also allowed, so this list is just a convenience, not a limit.
AUDIENCE_OPTIONS = [
    "All",
    "Boys 6-12",
    "Girls 6-12",
    "Boys 13-17",
    "Girls 13-17",
    "Adults 18-34",
    "Adults 35+",
]


# --------------------------------------------------------------------------
# Loading & cleaning
# --------------------------------------------------------------------------

def _clean_number(val) -> float:
    """Turn '101,597', ' --', '', NaN into a float (NaN if not applicable)."""
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if s in ("--", "-", "", "N/A", "n/a"):
        return np.nan
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return np.nan


def _clean_percent(val) -> float:
    """Turn '0.91%', ' --' into a fraction (0.0091). NaN if not applicable."""
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if s in ("--", "-", "", "N/A", "n/a"):
        return np.nan
    s = s.replace("%", "").replace(",", "")
    try:
        return float(s) / 100.0
    except ValueError:
        return np.nan


def load_campaign_csv(file_or_path) -> pd.DataFrame:
    """
    Load a raw Google-Ads-style campaign report export and return a fully
    cleaned dataframe with numeric types.

    Accepts a path, a file-like object, or raw bytes/str content.
    Automatically detects and skips the "Campaign report" title + date
    rows that precede the real header if present.
    """
    if isinstance(file_or_path, (bytes, bytearray)):
        raw_text = file_or_path.decode("utf-8", errors="replace")
    elif hasattr(file_or_path, "read"):
        content = file_or_path.read()
        raw_text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    else:
        with open(file_or_path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()

    # Detect and skip a title/date preamble (e.g. "Campaign report" /
    # "July 27, 2026 - August 5, 2026") that precedes the real header row.
    lines = raw_text.splitlines()
    skiprows = 0
    found_header = False
    for line in lines[:10]:
        if line.strip().startswith("Campaign,") or "Budget type" in line:
            found_header = True
            break
        skiprows += 1
    if not found_header:
        skiprows = 0  # header not found in first 10 lines; assume no preamble

    buf = io.StringIO(raw_text)
    df = pd.read_csv(buf, skiprows=skiprows)
    df = df.dropna(axis=1, how="all")
    df.columns = [c.strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Uploaded file is missing required columns: {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    # Clean numeric / comma-formatted columns
    for col in THOUSANDS_COLS + NUMERIC_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_clean_number)

    # Clean percentage columns
    for col in PERCENT_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_clean_percent)

    # Drop fully-empty rows (can happen with trailing blank lines)
    df = df.dropna(subset=["Campaign", "Cost"], how="any")

    # Audience is an optional column. If the file doesn't have one (most
    # exports won't), default every campaign to "All" -- the app lets the
    # user tag individual campaigns with a real audience afterwards.
    audience_col = next((c for c in df.columns if c.strip().lower() in ("audience", "target audience")), None)
    if audience_col:
        df = df.rename(columns={audience_col: "Audience"})
        df["Audience"] = df["Audience"].fillna("All")
    else:
        df["Audience"] = "All"

    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Currency conversion
# --------------------------------------------------------------------------

# Fallback rate used if a live rate can't be fetched (no internet, API
# down, etc). Update this periodically -- it's only a safety net.
FALLBACK_VND_TO_JPY = 0.0061  # approx JPY per 1 VND


def fetch_live_fx_rate(base: str = "VND", target: str = "JPY") -> tuple[float | None, str]:
    """
    Try to fetch a live FX rate (units of `target` per 1 unit of `base`).
    Returns (rate, source_description). rate is None if the fetch failed
    (e.g. no network access in this environment) -- caller should fall
    back to a manual/default rate in that case.
    """
    if requests is None:
        return None, "requests library not available"
    try:
        resp = requests.get(
            f"https://api.frankfurter.app/latest?from={base}&to={target}",
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = data.get("rates", {}).get(target)
        if rate:
            return float(rate), f"live rate via frankfurter.app ({data.get('date', '')})"
    except Exception as e:  # noqa: BLE001 -- network can fail many ways
        return None, f"fetch failed ({e})"
    return None, "rate not found in response"


def get_fx_rate(base: str = "VND", target: str = "JPY", manual_override: float | None = None):
    """
    Returns (rate, source_label). If manual_override is given, uses that.
    Otherwise attempts a live fetch, falling back to a hardcoded constant.
    """
    if manual_override is not None:
        return manual_override, "manual override"
    rate, source = fetch_live_fx_rate(base, target)
    if rate is not None:
        return rate, source
    return FALLBACK_VND_TO_JPY, f"fallback constant ({source})"


def add_jpy_columns(df: pd.DataFrame, fx_rate: float, cost_currency: str = "VND") -> pd.DataFrame:
    """
    Adds JPY-converted cost/rate columns based on the given fx_rate
    (units of JPY per 1 unit of cost_currency). Leaves original columns intact.
    """
    df = df.copy()
    df["FX Rate (to JPY)"] = fx_rate
    df["Cost (JPY)"] = df["Cost"] * fx_rate
    if "Avg. CPM" in df.columns:
        df["Avg. CPM (JPY)"] = df["Avg. CPM"] * fx_rate
    if "TrueView avg. CPV" in df.columns:
        df["TrueView avg. CPV (JPY)"] = df["TrueView avg. CPV"] * fx_rate
    return df


# --------------------------------------------------------------------------
# Historical benchmark rates
# --------------------------------------------------------------------------

@dataclass
class ChannelBenchmark:
    """Weighted-average historical performance benchmark for one
    (Campaign type, Bid strategy type, Audience) segment, in JPY."""

    segment: str
    bid_strategy: str
    audience: str
    n_campaigns: int
    total_cost_jpy: float
    total_impressions: float
    ecpm_jpy: float               # cost per 1000 impressions (flat historical average)
    cpc_jpy: float                # cost per click
    cpv_jpy: float                # cost per TrueView view
    ctr: float                    # clicks / impressions
    view_rate: float              # TrueView views / impressions
    vcr_25: float
    vcr_50: float
    vcr_75: float
    vcr_100: float                # video completion rate
    cpcv_jpy: float                # cost per completed view (100%)
    uu_rate: float                 # unique users / impressions
    # Spend -> eCPM curve (log-linear fit): ecpm = intercept + slope * ln(cost_jpy)
    # None if there isn't enough historical data (< 3 campaigns) to fit one
    # reliably -- in that case the simulator falls back to the flat ecpm_jpy.
    ecpm_curve_intercept: float = np.nan
    ecpm_curve_slope: float = np.nan
    ecpm_curve_r2: float = np.nan
    has_ecpm_curve: bool = False

    def as_dict(self):
        return self.__dict__


def _fit_ecpm_curve(g: pd.DataFrame):
    """
    Fit eCPM (per campaign) as a log-linear function of spend:
        ecpm = intercept + slope * ln(cost_jpy)
    using ordinary least squares on the individual campaigns within a
    segment. Requires at least 3 campaigns with valid cost & eCPM data to
    be considered reliable; otherwise returns all-NaN / has_curve=False.

    A positive slope means eCPM tends to rise as spend increases (more
    auction pressure at higher budgets); negative means it falls
    (efficiencies of scale). Either is plausible depending on the channel.
    """
    sub = g.dropna(subset=["Cost (JPY)", "Impr."])
    sub = sub[(sub["Cost (JPY)"] > 0) & (sub["Impr."] > 0)]
    if len(sub) < 3:
        return np.nan, np.nan, np.nan, False

    x = np.log(sub["Cost (JPY)"].values)
    y = (sub["Cost (JPY)"].values / sub["Impr."].values * 1000)  # per-campaign eCPM

    if np.std(x) == 0:
        return np.nan, np.nan, np.nan, False

    slope, intercept = np.polyfit(x, y, 1)
    y_pred = intercept + slope * x
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan

    return float(intercept), float(slope), float(r2), True


def project_ecpm(benchmark: dict, budget_jpy: float) -> float:
    """
    Given a benchmark dict (one row of compute_benchmarks()) and a
    candidate budget, return the projected eCPM at that spend level.
    Uses the fitted log-linear curve when available and budget is
    positive; otherwise falls back to the flat historical average eCPM.
    """
    flat = benchmark.get("ecpm_jpy", np.nan)
    if not budget_jpy or budget_jpy <= 0:
        return flat
    if benchmark.get("has_ecpm_curve") and pd.notna(benchmark.get("ecpm_curve_slope")):
        intercept = benchmark["ecpm_curve_intercept"]
        slope = benchmark["ecpm_curve_slope"]
        projected = intercept + slope * np.log(budget_jpy)
        # Guard against nonsensical (negative/zero) projections outside the
        # range of observed data -- fall back to the flat rate if so.
        if projected and projected > 0:
            return float(projected)
    return flat


def _weighted_avg(df, value_col, weight_col):
    sub = df[[value_col, weight_col]].dropna()
    if sub.empty or sub[weight_col].sum() == 0:
        return np.nan
    return float(np.average(sub[value_col], weights=sub[weight_col]))


def compute_benchmarks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Groups the cleaned+JPY-converted dataframe by (Campaign type, Bid
    strategy type) and computes weighted-average historical rates.
    Returns a tidy dataframe, one row per segment.
    """
    if "Audience" not in df.columns:
        df = df.copy()
        df["Audience"] = "All"

    rows = []
    group_cols = ["Campaign type", "Bid strategy type", "Audience"]
    for (seg, strategy, audience), g in df.groupby(group_cols):
        total_cost = g["Cost (JPY)"].sum()
        total_impr = g["Impr."].sum()
        total_clicks = g["Clicks"].sum() if "Clicks" in g else np.nan
        total_views = g["TrueView views"].sum() if "TrueView views" in g else np.nan
        total_uu = g["Unique users"].sum() if "Unique users" in g else np.nan

        ecpm = (total_cost / total_impr * 1000) if total_impr else np.nan
        cpc = (total_cost / total_clicks) if total_clicks else np.nan
        cpv = (total_cost / total_views) if total_views else np.nan
        ctr = (total_clicks / total_impr) if total_impr else np.nan
        view_rate = (total_views / total_impr) if total_impr else np.nan
        uu_rate = (total_uu / total_impr) if total_impr else np.nan

        vcr_25 = _weighted_avg(g, "Video played to 25%", "Impr.")
        vcr_50 = _weighted_avg(g, "Video played to 50%", "Impr.")
        vcr_75 = _weighted_avg(g, "Video played to 75%", "Impr.")
        vcr_100 = _weighted_avg(g, "Video played to 100%", "Impr.")

        completed_views = total_impr * vcr_100 if pd.notna(vcr_100) else np.nan
        cpcv = (total_cost / completed_views) if completed_views else np.nan

        intercept, slope, r2, has_curve = _fit_ecpm_curve(g)

        rows.append(
            ChannelBenchmark(
                segment=seg,
                bid_strategy=strategy,
                audience=audience,
                n_campaigns=len(g),
                total_cost_jpy=total_cost,
                total_impressions=total_impr,
                ecpm_jpy=ecpm,
                cpc_jpy=cpc,
                cpv_jpy=cpv,
                ctr=ctr,
                view_rate=view_rate,
                vcr_25=vcr_25,
                vcr_50=vcr_50,
                vcr_75=vcr_75,
                vcr_100=vcr_100,
                cpcv_jpy=cpcv,
                uu_rate=uu_rate,
                ecpm_curve_intercept=intercept,
                ecpm_curve_slope=slope,
                ecpm_curve_r2=r2,
                has_ecpm_curve=has_curve,
            ).as_dict()
        )

    return pd.DataFrame(rows)
