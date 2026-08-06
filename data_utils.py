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
# Flexible column mapping
# --------------------------------------------------------------------------
# Different exports name the same thing differently (e.g. "Gross Budget" vs
# "Cost" vs "Spend"). CANONICAL_SCHEMA maps each internal field name to a
# list of aliases (checked case/punctuation-insensitively) used to
# auto-detect which uploaded column maps to which internal field.
#
# Only fields with required=True must be mapped for the file to be usable;
# everything else is optional and simply won't contribute to the benchmark
# if absent (e.g. a file with no video-completion columns just won't have
# a VCR benchmark -- clicks/impressions/etc. still work fine).

CANONICAL_SCHEMA = {
    "Campaign": {
        "aliases": ["campaign", "campaign name", "line item", "ad group", "insertion order"],
        "required": False,
        "kind": "text",
    },
    "Campaign type": {
        "aliases": [
            "campaign type", "partner", "creative format", "channel",
            "platform", "media type", "publisher",
        ],
        "required": True,
        "kind": "text",
    },
    "Currency code": {
        "aliases": ["currency code", "currency", "curr"],
        "required": False,
        "kind": "text",
    },
    "Cost": {
        "aliases": ["cost", "gross budget", "spend", "budget", "net cost", "media cost", "net spend"],
        "required": True,
        "kind": "number",
    },
    "Bid strategy type": {
        "aliases": ["bid strategy type", "buying method", "bid strategy", "buy type", "pricing model"],
        "required": True,
        "kind": "text",
    },
    "Impr.": {
        "aliases": ["impr.", "impressions", "impr", "delivered impressions"],
        "required": True,
        "kind": "number",
    },
    "TrueView views": {
        "aliases": ["trueview views", "views", "video views"],
        "required": False,
        "kind": "number",
    },
    "Clicks": {
        "aliases": ["clicks"],
        "required": False,
        "kind": "number",
    },
    "CTR": {
        "aliases": ["ctr", "click through rate", "click-through rate"],
        "required": False,
        "kind": "percent",
    },
    "Video played to 25%": {
        "aliases": ["video played to 25%", "vcr 25%", "25% completion", "video completions 25%"],
        "required": False,
        "kind": "percent",
    },
    "Video played to 50%": {
        "aliases": ["video played to 50%", "vcr 50%", "50% completion", "video completions 50%", "midpoint"],
        "required": False,
        "kind": "percent",
    },
    "Video played to 75%": {
        "aliases": ["video played to 75%", "vcr 75%", "75% completion", "video completions 75%"],
        "required": False,
        "kind": "percent",
    },
    "Video played to 100%": {
        "aliases": [
            "video played to 100%", "vcr 100%", "100% completion", "video completions 100%",
            "video complete", "completion rate", "vcr",
        ],
        "required": False,
        "kind": "percent",
    },
    "Unique users": {
        "aliases": ["unique users", "uu", "reach", "unique reach"],
        "required": False,
        "kind": "number",
    },
    "Audience": {
        "aliases": ["audience", "target audience", "demo", "demographic"],
        "required": False,
        "kind": "text",
    },
}

REQUIRED_COLS = [k for k, v in CANONICAL_SCHEMA.items() if v["required"]]


def _normalize_header(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).strip().lower()).strip()


def suggest_column_mapping(columns: list[str]) -> dict[str, str | None]:
    """
    Given the raw column headers from an uploaded file, guess which
    canonical field each maps to, using exact alias matches first, falling
    back to substring containment. Returns {canonical_field: actual_column
    or None}. A column is used for at most one canonical field (first
    field to claim it wins, in CANONICAL_SCHEMA order).
    """
    normalized = {c: _normalize_header(c) for c in columns}
    claimed = set()
    mapping = {}

    for field, spec in CANONICAL_SCHEMA.items():
        alias_set = {_normalize_header(a) for a in spec["aliases"]}
        match = None
        # exact normalized match first
        for col, norm in normalized.items():
            if col in claimed:
                continue
            if norm in alias_set:
                match = col
                break
        # substring fallback
        if match is None:
            for col, norm in normalized.items():
                if col in claimed:
                    continue
                if any(alias in norm or norm in alias for alias in alias_set):
                    match = col
                    break
        mapping[field] = match
        if match:
            claimed.add(match)

    return mapping


def _normalize_bid_strategy(val) -> str:
    """Map free-text buying-method values to 'Target CPM' / 'Target CPV'."""
    if pd.isna(val):
        return np.nan
    s = str(val).strip().lower()
    if "cpv" in s:
        return "Target CPV"
    if "cpm" in s:
        return "Target CPM"
    return str(val).strip()  # unrecognized -- keep as-is, shown to user as a distinct segment


def apply_column_mapping(df_raw: pd.DataFrame, mapping: dict[str, str | None]) -> pd.DataFrame:
    """
    Rename df_raw's columns to canonical field names per `mapping`
    ({canonical: actual_column_or_None}), then clean/coerce types
    according to CANONICAL_SCHEMA. Missing optional fields are created as
    all-NaN columns so downstream code can rely on their presence.
    Missing required fields raise ValueError -- check before calling, or
    let the app catch this.
    """
    missing_required = [f for f in REQUIRED_COLS if not mapping.get(f)]
    if missing_required:
        raise ValueError(
            f"Missing required column(s), even after auto-matching: {missing_required}. "
            "Please map them manually."
        )

    df = pd.DataFrame(index=df_raw.index)
    for field, spec in CANONICAL_SCHEMA.items():
        src_col = mapping.get(field)
        if src_col and src_col in df_raw.columns:
            series = df_raw[src_col]
        else:
            series = pd.Series([np.nan] * len(df_raw), index=df_raw.index)

        if spec["kind"] == "number":
            df[field] = series.apply(_clean_number)
        elif spec["kind"] == "percent":
            df[field] = series.apply(_clean_percent)
        else:
            df[field] = series

    # Bid strategy normalization (CPV -> Target CPV, CPM -> Target CPM)
    df["Bid strategy type"] = df["Bid strategy type"].apply(_normalize_bid_strategy)

    # Synthesize a Campaign identifier if none was mapped/present
    if df["Campaign"].isna().all():
        df["Campaign"] = [f"Campaign {i + 1}" for i in range(len(df))]
    else:
        df["Campaign"] = df["Campaign"].fillna(
            pd.Series([f"Campaign {i + 1}" for i in range(len(df))], index=df.index)
        )

    # Audience defaults to "All" if not present/mapped
    df["Audience"] = df["Audience"].fillna("All")
    df.loc[df["Audience"].astype(str).str.strip() == "", "Audience"] = "All"

    # Drop fully-empty rows (blank trailing lines etc.)
    df = df.dropna(subset=["Campaign type", "Cost"], how="any")

    return df.reset_index(drop=True)


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


def read_raw_csv(file_or_path) -> pd.DataFrame:
    """
    Read a CSV that may have a title/date preamble before the real header
    row (common in ad-platform exports), without assuming any particular
    column names. Detects the header row by finding the row within the
    first 15 lines whose fields best match known canonical-field aliases.
    Returns the raw dataframe with original (stripped) column headers --
    no cleaning or renaming applied yet.
    """
    if isinstance(file_or_path, (bytes, bytearray)):
        raw_text = file_or_path.decode("utf-8", errors="replace")
    elif hasattr(file_or_path, "read"):
        content = file_or_path.read()
        raw_text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    else:
        with open(file_or_path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()

    lines = raw_text.splitlines()
    all_aliases = {
        _normalize_header(a) for spec in CANONICAL_SCHEMA.values() for a in spec["aliases"]
    }

    def score(line: str) -> int:
        fields = [_normalize_header(f) for f in line.split(",")]
        return sum(1 for f in fields if f in all_aliases)

    best_idx, best_score = 0, -1
    for i, line in enumerate(lines[:15]):
        s = score(line)
        if s > best_score:
            best_score, best_idx = s, i

    skiprows = best_idx if best_score >= 2 else 0  # need at least 2 recognizable headers to trust it

    buf = io.StringIO(raw_text)
    df = pd.read_csv(buf, skiprows=skiprows)
    df = df.dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")  # drop fully blank rows
    return df.reset_index(drop=True)


def load_campaign_csv(file_or_path, mapping_override: dict[str, str | None] | None = None) -> pd.DataFrame:
    """
    Convenience one-shot loader: reads the file, auto-suggests a column
    mapping (or uses `mapping_override` if given), applies it, and returns
    the fully cleaned canonical dataframe. Raises ValueError if a required
    field couldn't be mapped -- for a friendlier flow, use read_raw_csv() +
    suggest_column_mapping() + apply_column_mapping() directly so the app
    can show the user a mapping UI instead of failing outright.
    """
    df_raw = read_raw_csv(file_or_path)
    mapping = mapping_override or suggest_column_mapping(df_raw.columns.tolist())
    return apply_column_mapping(df_raw, mapping)


# --------------------------------------------------------------------------
# Currency conversion
# --------------------------------------------------------------------------

# Currencies supported in the app's dropdowns. "Other" lets a user type a
# custom ISO code if they need one that isn't listed.
SUPPORTED_CURRENCIES = ["VND", "KRW", "JPY", "USD"]

# Fallback rates used if a live rate can't be fetched (no internet, API
# down, etc). Units: JPY per 1 unit of source currency. Update periodically
# -- these are only a safety net, not meant to be precise.
FALLBACK_RATES_TO_JPY = {
    "VND": 0.0061,
    "KRW": 0.11,
    "USD": 150.0,
    "JPY": 1.0,
}


def fetch_live_fx_rate(base: str, target: str) -> tuple[float | None, str]:
    """
    Try to fetch a live FX rate (units of `target` per 1 unit of `base`).
    Returns (rate, source_description). rate is None if the fetch failed
    (e.g. no network access in this environment) -- caller should fall
    back to a manual/default rate in that case.
    """
    if base == target:
        return 1.0, "same currency"
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


def get_fx_rate(base: str, target: str, manual_override: float | None = None):
    """
    Returns (rate, source_label) for a single currency pair. If
    manual_override is given, uses that. Otherwise attempts a live fetch,
    falling back to a hardcoded constant.
    """
    if manual_override is not None:
        return manual_override, "manual override"
    rate, source = fetch_live_fx_rate(base, target)
    if rate is not None:
        return rate, source
    fallback = FALLBACK_RATES_TO_JPY.get(base)
    if fallback is not None and target == "JPY":
        return fallback, f"fallback constant ({source})"
    return None, f"no rate available ({source})"


def get_fx_rates_for_currencies(
    currencies: list[str], target: str, manual_overrides: dict[str, float] | None = None
) -> dict[str, tuple[float, str]]:
    """
    Fetch/resolve a rate for each currency in `currencies` -> `target`.
    manual_overrides: optional dict of {currency: rate} to force specific
    currencies to a manual rate instead of fetching live.
    Returns {currency: (rate, source_label)}.
    """
    manual_overrides = manual_overrides or {}
    results = {}
    for cur in currencies:
        override = manual_overrides.get(cur)
        results[cur] = get_fx_rate(cur, target, manual_override=override)
    return results


def add_converted_columns(
    df: pd.DataFrame,
    fx_rates: dict[str, float],
    target_currency: str = "JPY",
    currency_col: str = "Currency code",
) -> pd.DataFrame:
    """
    Adds target-currency-converted cost/rate columns, converting each row
    using its OWN currency (from `currency_col`) and the matching rate in
    `fx_rates` (a dict of {currency_code: rate_to_target}). This supports
    files where different campaigns are booked in different currencies.

    Rows whose currency isn't in `fx_rates` get NaN for the converted
    columns (rather than silently using the wrong rate).
    """
    df = df.copy()
    row_rate = df[currency_col].map(fx_rates)
    missing = df.loc[row_rate.isna(), currency_col].unique()
    df[f"FX Rate (to {target_currency})"] = row_rate
    df[f"Cost ({target_currency})"] = df["Cost"] * row_rate
    if "Avg. CPM" in df.columns:
        df[f"Avg. CPM ({target_currency})"] = df["Avg. CPM"] * row_rate
    if "TrueView avg. CPV" in df.columns:
        df[f"TrueView avg. CPV ({target_currency})"] = df["TrueView avg. CPV"] * row_rate
    df.attrs["missing_fx_currencies"] = list(missing)
    return df


def add_jpy_columns(df: pd.DataFrame, fx_rate: float, cost_currency: str = "VND") -> pd.DataFrame:
    """
    Backwards-compatible single-currency conversion (assumes the whole
    file is in `cost_currency`). Prefer add_converted_columns() for files
    that may mix currencies -- this is kept for simplicity when a caller
    already knows the whole file is one currency.
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


def _fit_ecpm_curve(g: pd.DataFrame, cost_col: str = "Cost (JPY)"):
    """
    Fit eCPM (per campaign) as a log-linear function of spend:
        ecpm = intercept + slope * ln(cost)
    using ordinary least squares on the individual campaigns within a
    segment. `cost_col` should be the cost column already converted into
    whatever target currency the app is using (its values represent spend
    in that currency, regardless of the column's literal name).
    Requires at least 3 campaigns with valid cost & eCPM data to be
    considered reliable; otherwise returns all-NaN / has_curve=False.

    A positive slope means eCPM tends to rise as spend increases (more
    auction pressure at higher budgets); negative means it falls
    (efficiencies of scale). Either is plausible depending on the channel.
    """
    sub = g.dropna(subset=[cost_col, "Impr."])
    sub = sub[(sub[cost_col] > 0) & (sub["Impr."] > 0)]
    if len(sub) < 3:
        return np.nan, np.nan, np.nan, False

    x = np.log(sub[cost_col].values)
    y = (sub[cost_col].values / sub["Impr."].values * 1000)  # per-campaign eCPM

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


def compute_benchmarks(df: pd.DataFrame, cost_col: str = "Cost (JPY)") -> pd.DataFrame:
    """
    Groups the cleaned + currency-converted dataframe by (Campaign type,
    Bid strategy type, Audience) and computes weighted-average historical
    rates. `cost_col` should be the cost column already converted into
    whatever target currency the app is using -- pass e.g. "Cost (USD)"
    if that's what add_converted_columns() produced. All resulting rate
    fields (ecpm_jpy, cpc_jpy, etc.) are expressed in that target currency
    regardless of the field name (kept as "_jpy" for backward compatibility
    with existing code -- treat it as "in target currency").
    Returns a tidy dataframe, one row per segment.
    """
    if "Audience" not in df.columns:
        df = df.copy()
        df["Audience"] = "All"

    rows = []
    group_cols = ["Campaign type", "Bid strategy type", "Audience"]
    for (seg, strategy, audience), g in df.groupby(group_cols):
        total_cost = g[cost_col].sum()
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

        intercept, slope, r2, has_curve = _fit_ecpm_curve(g, cost_col=cost_col)

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
