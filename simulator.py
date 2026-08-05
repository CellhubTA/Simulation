"""
simulator.py
-------------
Projection logic that turns a historical benchmark row (see
data_utils.compute_benchmarks) into forward-looking campaign estimates.

Two directions are supported:
  - simulate_from_budget(): budget -> projected impressions/clicks/etc.
  - simulate_from_target(): target metric -> required budget + full projection

Both respect the campaign's Bid strategy type:
  - "Target CPM": buying is driven off eCPM (cost per 1000 impressions).
    Impressions are the primary lever; everything else (clicks, views,
    completions, unique users) is derived from historical rates applied
    to those impressions.
  - "Target CPV": buying is driven off CPV (cost per TrueView view).
    TrueView views are the primary lever; impressions are backed out
    using the historical view rate, then the same downstream rates apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

TARGETABLE_METRICS = [
    "Impressions",
    "TrueView views",
    "Clicks",
    "Video completions (100%)",
    "Unique users",
    "Budget (JPY)",
]


@dataclass
class ProjectionResult:
    bid_strategy: str
    budget_jpy: float
    impressions: float
    clicks: float
    trueview_views: float
    video_25: float
    video_50: float
    video_75: float
    video_100: float  # completions
    unique_users: float
    ctr: float
    vcr_100: float
    ecpm_jpy: float
    cpc_jpy: float
    cpv_jpy: float
    cpcv_jpy: float
    warnings: list = field(default_factory=list)

    def as_dict(self):
        d = self.__dict__.copy()
        return d


def _safe(v, default=np.nan):
    return default if v is None else v


def simulate_from_budget(benchmark: dict, budget_jpy: float) -> ProjectionResult:
    """
    Project campaign performance for a given JPY budget, using the rates
    in `benchmark` (one row from compute_benchmarks(), as a dict).
    """
    strategy = benchmark.get("bid_strategy", "Target CPM")
    warnings = []

    ecpm = benchmark.get("ecpm_jpy", np.nan)
    cpv = benchmark.get("cpv_jpy", np.nan)
    ctr = benchmark.get("ctr", np.nan)
    view_rate = benchmark.get("view_rate", np.nan)
    vcr_25 = benchmark.get("vcr_25", np.nan)
    vcr_50 = benchmark.get("vcr_50", np.nan)
    vcr_75 = benchmark.get("vcr_75", np.nan)
    vcr_100 = benchmark.get("vcr_100", np.nan)
    uu_rate = benchmark.get("uu_rate", np.nan)

    if strategy == "Target CPV":
        if not cpv or np.isnan(cpv):
            warnings.append(
                "No historical Target CPV data for this segment -- "
                "falling back to Target CPM-derived eCPM to estimate impressions."
            )
            impressions = (budget_jpy / ecpm * 1000) if ecpm else np.nan
            trueview_views = impressions * view_rate if view_rate else np.nan
        else:
            trueview_views = budget_jpy / cpv
            impressions = (
                trueview_views / view_rate if view_rate else np.nan
            )
            if not view_rate or np.isnan(view_rate):
                warnings.append(
                    "No historical view-rate available; impressions could not be derived."
                )
    else:  # Target CPM (default)
        if not ecpm or np.isnan(ecpm):
            warnings.append("No historical Target CPM data for this segment.")
            impressions = np.nan
        else:
            impressions = budget_jpy / ecpm * 1000
        trueview_views = impressions * view_rate if (view_rate and impressions) else np.nan

    clicks = impressions * ctr if (ctr and impressions) else np.nan
    video_25 = impressions * vcr_25 if (vcr_25 and impressions) else np.nan
    video_50 = impressions * vcr_50 if (vcr_50 and impressions) else np.nan
    video_75 = impressions * vcr_75 if (vcr_75 and impressions) else np.nan
    video_100 = impressions * vcr_100 if (vcr_100 and impressions) else np.nan
    unique_users = impressions * uu_rate if (uu_rate and impressions) else np.nan

    cpc = budget_jpy / clicks if clicks else np.nan
    cpcv = budget_jpy / video_100 if video_100 else np.nan
    ecpm_out = (budget_jpy / impressions * 1000) if impressions else np.nan
    cpv_out = (budget_jpy / trueview_views) if trueview_views else np.nan

    return ProjectionResult(
        bid_strategy=strategy,
        budget_jpy=budget_jpy,
        impressions=impressions,
        clicks=clicks,
        trueview_views=trueview_views,
        video_25=video_25,
        video_50=video_50,
        video_75=video_75,
        video_100=video_100,
        unique_users=unique_users,
        ctr=ctr,
        vcr_100=vcr_100,
        ecpm_jpy=ecpm_out,
        cpc_jpy=cpc,
        cpv_jpy=cpv_out,
        cpcv_jpy=cpcv,
        warnings=warnings,
    )


def simulate_from_target(benchmark: dict, target_metric: str, target_value: float) -> ProjectionResult:
    """
    Back into the required budget (and full projection) needed to hit a
    given target for one metric: Impressions, TrueView views, Clicks,
    Video completions (100%), or Unique users.
    """
    strategy = benchmark.get("bid_strategy", "Target CPM")
    ecpm = benchmark.get("ecpm_jpy", np.nan)
    cpv = benchmark.get("cpv_jpy", np.nan)
    ctr = benchmark.get("ctr", np.nan)
    view_rate = benchmark.get("view_rate", np.nan)
    vcr_100 = benchmark.get("vcr_100", np.nan)
    uu_rate = benchmark.get("uu_rate", np.nan)

    warnings = []

    # Step 1: convert the target metric into an implied impressions figure
    if target_metric == "Impressions":
        impressions = target_value
    elif target_metric == "TrueView views":
        impressions = target_value / view_rate if view_rate else np.nan
    elif target_metric == "Clicks":
        impressions = target_value / ctr if ctr else np.nan
    elif target_metric == "Video completions (100%)":
        impressions = target_value / vcr_100 if vcr_100 else np.nan
    elif target_metric == "Unique users":
        impressions = target_value / uu_rate if uu_rate else np.nan
    elif target_metric == "Budget (JPY)":
        # Direct pass-through: same as simulate_from_budget
        return simulate_from_budget(benchmark, target_value)
    else:
        raise ValueError(f"Unknown target metric: {target_metric}")

    if impressions is None or (isinstance(impressions, float) and np.isnan(impressions)):
        warnings.append(
            f"Could not derive impressions from target '{target_metric}' -- "
            "missing historical rate for this segment."
        )
        impressions = np.nan

    # Step 2: convert impressions into required budget under the bid strategy
    if strategy == "Target CPV":
        if cpv and not np.isnan(cpv) and view_rate:
            trueview_views = impressions * view_rate
            budget = trueview_views * cpv
        else:
            warnings.append(
                "No historical Target CPV data -- falling back to eCPM to estimate budget."
            )
            budget = impressions / 1000 * ecpm if ecpm else np.nan
    else:
        budget = impressions / 1000 * ecpm if ecpm else np.nan

    result = simulate_from_budget(benchmark, budget if budget and not np.isnan(budget) else np.nan)
    result.warnings = warnings + result.warnings
    return result


def allocate_budget_across_segments(
    benchmarks_df,
    total_budget_jpy: float,
    allocations: dict,
) -> list[ProjectionResult]:
    """
    allocations: dict mapping a (Campaign type, Bid strategy type) tuple
    (or a row index into benchmarks_df) to a percentage (0-1) of total budget.
    Returns a list of ProjectionResult, one per allocated segment.
    """
    results = []
    for key, pct in allocations.items():
        if pct <= 0:
            continue
        row = benchmarks_df.loc[
            (benchmarks_df["segment"] == key[0]) & (benchmarks_df["bid_strategy"] == key[1])
        ]
        if row.empty:
            continue
        benchmark = row.iloc[0].to_dict()
        seg_budget = total_budget_jpy * pct
        res = simulate_from_budget(benchmark, seg_budget)
        res_dict = res.as_dict()
        res_dict["segment"] = key[0]
        results.append(res_dict)
    return results
