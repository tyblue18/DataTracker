"""Trend & progress computations for Ironman training.

These functions take the DataFrames returned by ``db.load_*`` and return tidy
DataFrames ready to chart. Pure pandas, no Garmin calls.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

# Training-load smoothing constants (TrainingPeaks-style fitness/fatigue model).
# CTL ("Fitness") = 42-day exponentially weighted load.
# ATL ("Fatigue") = 7-day exponentially weighted load.
# TSB ("Form")    = yesterday's CTL - yesterday's ATL.
_ALPHA_CTL = 1 - math.exp(-1 / 42)
_ALPHA_ATL = 1 - math.exp(-1 / 7)

# CTL needs roughly one time constant of history before it means anything. Days
# before this are the model filling up from zero, not fitness being built, so we
# mark them and refuse to read a ramp rate off them.
CTL_WARMUP_DAYS = 42

HR_ZONE_COLS = [f"hr_z{i}_s" for i in range(1, 6)]

# Monotony is unbounded above; cap it so one flat week doesn't rescale the axis.
MONOTONY_CAP = 5.0

# Where the intensity bands sit. Z1-Z2 is "low" (below the first threshold),
# Z3 is "moderate" (the threshold/tempo band), Z4-Z5 is "high".
# This 3-band split is the convention used in the training-intensity-distribution
# literature (Seiler), which is what makes a distribution comparable to the
# published polarised / pyramidal models.
_BAND_OF_ZONE = {1: "low", 2: "low", 3: "moderate", 4: "high", 5: "high"}


def _today() -> pd.Timestamp:
    return pd.Timestamp(date.today())


def _ewma(x: pd.Series, alpha: float, seed: float) -> pd.Series:
    """Exponentially weighted mean with an explicit initial value.

    ``Series.ewm(adjust=False)`` seeds at the first observation, which for daily
    training load means the entire fitness curve is anchored to whatever
    happened on day one.
    """
    out = np.empty(len(x), dtype="float64")
    prev = float(seed)
    for i, v in enumerate(x.to_numpy(dtype="float64")):
        prev += alpha * (np.nan_to_num(v) - prev)
        out[i] = prev
    return pd.Series(out, index=x.index)


def weekly_volume(activities: pd.DataFrame) -> pd.DataFrame:
    """Distance / time / load per ISO week, per sport.

    Returns columns: week_start, sport, distance_km, hours, training_load, sessions.
    """
    if activities.empty:
        return pd.DataFrame(
            columns=["week_start", "sport", "distance_km", "hours",
                     "training_load", "sessions"]
        )
    df = activities.copy()
    df["date"] = pd.to_datetime(df["date"])
    # Monday of each activity's week.
    df["week_start"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.normalize()
    grp = df.groupby(["week_start", "sport"]).agg(
        distance_km=("distance_m", lambda s: s.sum() / 1000.0),
        hours=("duration_s", lambda s: s.sum() / 3600.0),
        training_load=("training_load", "sum"),
        sessions=("activity_id", "count"),
    ).reset_index()
    return grp.sort_values("week_start")


def daily_load(activities: pd.DataFrame, through: pd.Timestamp | None = None) -> pd.DataFrame:
    """Total training load per calendar day, on a gap-free daily index.

    ``through`` extends the index past the last workout (default: today). This
    matters: rest days have to be carried forward for the exponential averages
    to decay. Ending the index at the last activity freezes fitness and fatigue
    at the moment you stopped training, which reads as "nothing changed" when
    what actually happened is detraining.
    """
    if activities.empty:
        return pd.DataFrame(columns=["date", "load"])
    df = activities.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    daily = df.groupby("date")["training_load"].sum()
    end = pd.Timestamp(through).normalize() if through is not None else _today()
    end = max(end, daily.index.max())
    full = pd.date_range(daily.index.min(), end, freq="D")
    daily = daily.reindex(full, fill_value=0.0)
    return daily.rename_axis("date").reset_index(name="load")


def fitness_fatigue(activities: pd.DataFrame,
                    through: pd.Timestamp | None = None) -> pd.DataFrame:
    """CTL (fitness), ATL (fatigue) and TSB (form) over time.

    Uses Garmin's per-activity training load as the daily stress input.
    Returns columns: date, load, ctl, atl, tsb, warmup.

    ``warmup`` is True for the first ``CTL_WARMUP_DAYS`` days, where CTL is
    still climbing out of its zero seed. During that stretch CTL rises even on
    an unchanged training load, so any ramp-rate read off it is an artefact of
    the filter, not a training effect.
    """
    cols = ["date", "load", "ctl", "atl", "tsb", "warmup"]
    dl = daily_load(activities, through=through)
    if dl.empty:
        return pd.DataFrame(columns=cols)
    dl = dl.set_index("date")
    # Seed both averages at the average daily load of the opening fortnight.
    # pandas' ``adjust=False`` would otherwise seed at day one's load — a single
    # arbitrary session (or a rest day, i.e. zero) deciding where the whole
    # fitness curve starts. The seed can't manufacture missing history, so the
    # warm-up window is flagged rather than trusted.
    seed = float(dl["load"].head(14).mean())
    dl["ctl"] = _ewma(dl["load"], _ALPHA_CTL, seed)
    dl["atl"] = _ewma(dl["load"], _ALPHA_ATL, seed)
    # Form is the balance at the *start* of the day -> use yesterday's values.
    dl["tsb"] = (dl["ctl"] - dl["atl"]).shift(1)
    dl["warmup"] = np.arange(len(dl)) < CTL_WARMUP_DAYS
    return dl.reset_index()


def ramp_rate(ff: pd.DataFrame, window: int = 28) -> dict | None:
    """Current CTL change per week, with the accepted-risk band attached.

    The widely used guideline is that sustained CTL growth above ~5-7 points per
    week carries a raised injury/illness risk; flat-to-negative means fitness is
    being maintained or lost. Returns None while CTL is still warming up.
    """
    if ff.empty or ff["ctl"].isna().all():
        return None
    valid = ff[~ff["warmup"]]
    if len(valid) < window + 1:
        return None
    ctl = valid["ctl"]
    per_week = float(ctl.iloc[-1] - ctl.iloc[-window - 1]) / (window / 7.0)
    if per_week > 7:
        label, status = "Aggressive", "critical"
    elif per_week > 3:
        label, status = "Building", "good"
    elif per_week >= -1:
        label, status = "Maintaining", "warning"
    else:
        label, status = "Detraining", "serious"
    return {"per_week": round(per_week, 1), "label": label, "status": status,
            "ctl": round(float(ctl.iloc[-1]), 1), "window_days": window}


# ---------------------------------------------------------------------------
# Training-intensity distribution
# ---------------------------------------------------------------------------
# How training time splits across low / moderate / high intensity is one of the
# most consistently supported predictors of endurance adaptation. Published
# models for long-course triathlon cluster around:
#
#   Polarised  ~80% low /  ~5% moderate / ~15% high
#   Pyramidal  ~78% low / ~19% moderate /  ~3% high
#
# Both put roughly 3/4 or more of total time below the first threshold. A
# distribution with more time high than low ("inverted") is the pattern
# associated with stagnation and non-functional overreaching in endurance work.

TID_MODELS = {
    "polarised": (0.80, 0.05, 0.15),
    "pyramidal": (0.78, 0.19, 0.03),
}


# ---------------------------------------------------------------------------
# Heart-rate quality
# ---------------------------------------------------------------------------
# Wrist optical sensors fail in a specific, well-documented way during running:
# the sensor locks onto the motion artefact from foot strike and reports cadence
# as heart rate. It is not random noise — it is a plausible-looking number in the
# 160-180 range, which is exactly the range a hard run would produce. Nothing
# downstream can tell the difference, so it has to be caught here.
#
# Two independent tests, because neither is conclusive alone:
#
#   1. HR sitting on top of cadence. Suggestive, but weak on its own: a runner's
#      cadence (~170 spm) and a genuinely hard run's HR (~170 bpm) overlap
#      naturally, so proximity can be coincidence.
#   2. HR and power telling different stories about the same session. This is
#      the strong test. Running power is derived from pace and motion rather
#      than from the optical sensor, so it cannot lock in the same way. When HR
#      says most of a session was above threshold and power says almost none of
#      it was, one of them is wrong.

HR_CADENCE_TOLERANCE = 8.0    # bpm; |HR - cadence| below this is "sitting on it"
HR_POWER_DISAGREEMENT = 0.35  # share-of-hard-time gap that counts as a conflict
PWR_ZONE_COLS = [f"pwr_z{i}_s" for i in range(1, 6)]


def _hard_share(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """Fraction of zoned time spent in the top two zones."""
    present = [c for c in cols if c in df.columns]
    if len(present) < 5:
        return pd.Series(np.nan, index=df.index)
    z = df[present].apply(pd.to_numeric, errors="coerce")
    total = z.sum(axis=1, min_count=1)
    return z[present[3:]].sum(axis=1, min_count=1) / total.replace(0, np.nan)


def hr_quality(activities: pd.DataFrame) -> pd.DataFrame:
    """Per-activity heart-rate trustworthiness.

    Adds: hr_hard_share, pwr_hard_share, hr_cadence_gap, hr_cadence_close,
    hr_power_disagree, hr_suspect, hr_quality.

    ``hr_quality`` is one of "ok", "suspect" or "unknown". Only *running* is
    assessed — there is no foot strike to lock onto when cycling or swimming,
    and those HR traces look normal in practice.
    """
    out = activities.copy()
    if out.empty:
        for c in ("hr_hard_share", "pwr_hard_share", "hr_cadence_gap"):
            out[c] = pd.Series(dtype="float64")
        for c in ("hr_cadence_close", "hr_power_disagree", "hr_suspect"):
            out[c] = pd.Series(dtype="bool")
        out["hr_quality"] = pd.Series(dtype="object")
        return out

    out["hr_hard_share"] = _hard_share(out, HR_ZONE_COLS)
    out["pwr_hard_share"] = _hard_share(out, PWR_ZONE_COLS)

    cad = pd.to_numeric(out.get("avg_cadence"), errors="coerce") \
        if "avg_cadence" in out else pd.Series(np.nan, index=out.index)
    hr = pd.to_numeric(out.get("avg_hr"), errors="coerce")
    out["hr_cadence_gap"] = (hr - cad).abs()

    is_run = out["sport"] == "run"
    # Walking/hiking cadence is nowhere near HR, so the proximity test only
    # means anything at running cadences.
    out["hr_cadence_close"] = (is_run & (cad > 140) &
                               (out["hr_cadence_gap"] <= HR_CADENCE_TOLERANCE)).fillna(False)
    out["hr_power_disagree"] = (
        is_run & ((out["hr_hard_share"] - out["pwr_hard_share"]) >= HR_POWER_DISAGREEMENT)
    ).fillna(False)

    # When power zones exist they arbitrate, because the disagreement test is
    # the strong one. Proximity is only allowed to convict on its own when
    # there is no power data to check against — otherwise a genuinely hard run,
    # where HR legitimately sits near cadence, gets thrown out along with the
    # broken ones.
    has_power = out[[c for c in PWR_ZONE_COLS if c in out.columns]].notna().any(axis=1) \
        if any(c in out.columns for c in PWR_ZONE_COLS) \
        else pd.Series(False, index=out.index)
    out["hr_suspect"] = out["hr_power_disagree"] | (
        out["hr_cadence_close"] & out["hr_hard_share"].gt(0.5).fillna(False) & ~has_power)
    out["hr_quality"] = np.where(
        ~is_run, "ok",
        np.where(out["hr_suspect"], "suspect",
                 np.where(hr.notna(), "ok", "unknown")))
    return out


def hr_quality_summary(activities: pd.DataFrame,
                       tags: pd.DataFrame | None = None) -> dict | None:
    """How much of the running data has untrustworthy heart rate.

    Runs you have tagged by hand are counted as resolved: the sensor is still
    wrong, but the session is no longer being scored from it.
    """
    if activities.empty or "sport" not in activities.columns:
        return None
    q = hr_quality(apply_tags(activities, tags) if tags is not None else activities)
    runs = q[q["sport"] == "run"]
    if runs.empty:
        return None
    feel = runs["feel"] if "feel" in runs.columns else pd.Series(None, index=runs.index)
    resolved = feel.isin(FEEL_TO_ZONE)
    suspect = runs[runs["hr_suspect"] & ~resolved]
    return {
        "runs": int(len(runs)),
        "suspect": int(len(suspect)),
        "tagged": int(resolved.sum()),
        "unresolved": int(len(suspect)),
        "share": float(len(suspect) / len(runs)),
        "hr_hard_median": float(runs["hr_hard_share"].median(skipna=True) * 100)
        if runs["hr_hard_share"].notna().any() else None,
        "pwr_hard_median": float(runs["pwr_hard_share"].median(skipna=True) * 100)
        if runs["pwr_hard_share"].notna().any() else None,
        "has_power_zones": bool(runs[[c for c in PWR_ZONE_COLS
                                      if c in runs.columns]].notna().any().any()),
    }


# ---------------------------------------------------------------------------
# Subjective session tags (RPE + talk test)
# ---------------------------------------------------------------------------

FEEL_ORDER = ["easy", "moderate", "hard"]

# How a session you have labelled by hand is placed into zones. Deliberately
# blunt: a tag is a whole-session judgement, so it is applied to the whole
# session. "hard" keeps a warm-up/cool-down allowance in the lower zones,
# because no interval session is hard from the first step to the last.
#
# This is the manual override. Tag a run "easy" and it counts as easy no matter
# what the wrist sensor thought it saw — which is the correct precedence, since
# you were there and the sensor was inferring.
FEEL_TO_ZONE = {
    "easy":     {1: 0.45, 2: 0.55, 3: 0.00, 4: 0.00, 5: 0.00},
    "moderate": {1: 0.10, 2: 0.25, 3: 0.65, 4: 0.00, 5: 0.00},
    "hard":     {1: 0.10, 2: 0.15, 3: 0.20, 4: 0.45, 5: 0.10},
}


def apply_tags(activities: pd.DataFrame,
               tags: pd.DataFrame | None) -> pd.DataFrame:
    """Merge session tags onto activities, adding rpe, feel and srpe_load.

    Session-RPE load (RPE x minutes) is included because it is the load measure
    with the strongest validation behind it, and the only one here that is
    immune to every sensor problem — it comes from you, not the watch.
    """
    df = activities.copy()
    if df.empty:
        for c in ("rpe", "feel", "srpe_load"):
            df[c] = pd.Series(dtype="float64" if c != "feel" else "object")
        return df
    if tags is None or tags.empty or "activity_id" not in tags.columns:
        df["rpe"], df["feel"] = np.nan, None
    else:
        t = tags[["activity_id", "rpe", "feel"]].drop_duplicates("activity_id")
        df = df.merge(t, on="activity_id", how="left")
    df["srpe_load"] = pd.to_numeric(df["rpe"], errors="coerce") * \
        (pd.to_numeric(df["duration_s"], errors="coerce") / 60.0)
    return df


def tag_coverage(activities: pd.DataFrame, tags: pd.DataFrame | None,
                 sport: str | None = None) -> dict:
    """How much of the training is backed by your own read of it."""
    df = apply_tags(activities, tags)
    if sport:
        df = df[df["sport"] == sport]
    total = int(len(df))
    tagged = int(df["feel"].notna().sum()) if total else 0
    return {"total": total, "tagged": tagged,
            "share": (tagged / total) if total else 0.0}


def feel_vs_zones(activities: pd.DataFrame, tags: pd.DataFrame | None,
                  source: str = "auto") -> pd.DataFrame:
    """Where the sensor and your own read of a session disagree.

    Returns the tagged sessions with their band from time-in-zone next to the
    ``feel`` you recorded, and an ``agrees`` flag. Persistent disagreement in
    one direction points at the sensor, not at you.
    """
    cols = ["date", "sport", "feel", "rpe", "zone_source", "high_pct",
            "low_pct", "sensor_band", "agrees"]
    df = apply_tags(activities, tags)
    df = df[df["feel"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    z = zone_time(df, source=source)
    total = z["zone_total_s"].replace(0, np.nan)
    z["low_pct"] = z["low_s"] / total * 100
    z["high_pct"] = z["high_s"] / total * 100
    # Same 3-band language the talk test uses, so the two are comparable.
    z["sensor_band"] = np.select(
        [z["high_pct"] >= 35, z["low_pct"] >= 65],
        ["hard", "easy"], default="moderate")
    z.loc[total.isna(), "sensor_band"] = None
    z["agrees"] = z["sensor_band"] == z["feel"]
    return z[cols].sort_values("date")


def has_zone_data(activities: pd.DataFrame, source: str = "hr") -> bool:
    """True if any activity carries time-in-zone (needs a backfill/sync if not)."""
    if activities.empty:
        return False
    wanted = PWR_ZONE_COLS if source == "power" else HR_ZONE_COLS
    cols = [c for c in wanted if c in activities.columns]
    if source == "auto":
        cols += [c for c in PWR_ZONE_COLS if c in activities.columns]
    return bool(cols) and bool(activities[cols].notna().any().any())


def zone_time(activities: pd.DataFrame, source: str = "hr",
              tags: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-activity seconds in each zone plus low/moderate/high bands.

    Returns the input columns plus z1_s..z5_s, low_s, moderate_s, high_s,
    zone_total_s and zone_source. Activities without zone data get NaN, not
    zero, so they are excluded from shares rather than counted as all-easy.

    ``source``:
      "hr"    — heart-rate zones (the default, and correct with a good sensor).
      "power" — power zones. Immune to optical cadence lock, but an *external*
                load measure: it says what you produced, not what it cost you.
      "auto"  — your own ``feel`` tag where you set one, then HR zones, except
                for runs whose HR fails ``hr_quality``, which fall back to power
                where power zones exist. Mixed by design, so ``zone_source``
                records what each row actually used.

    A ``feel`` tag always wins under "auto". You were there; the sensor was
    guessing. See ``FEEL_TO_ZONE``.
    """
    if activities.empty:
        return activities
    df = activities.copy()
    if tags is not None and "feel" not in df.columns:
        df = apply_tags(df, tags)

    def _cols(prefix: str) -> list[str]:
        return [f"{prefix}_z{i}_s" for i in range(1, 6)]

    def _read(cols: list[str]) -> pd.DataFrame:
        # float64 throughout: integer zone seconds would reject the fractional
        # values a feel-tag override writes back.
        return pd.DataFrame(
            {i: (pd.to_numeric(df[c], errors="coerce").astype("float64")
                 if c in df else np.nan)
             for i, c in enumerate(cols, start=1)}, index=df.index, dtype="float64")

    hr_z, pwr_z = _read(_cols("hr")), _read(_cols("pwr"))

    if source == "power":
        chosen, df["zone_source"] = pwr_z, "power"
    elif source == "auto":
        q = hr_quality(df)
        use_power = q["hr_suspect"] & pwr_z.notna().any(axis=1)
        chosen = hr_z.where(~use_power, pwr_z)
        src = np.where(use_power, "power", "hr")

        # Your own read of the session overrides both sensors.
        feel = df["feel"] if "feel" in df.columns else pd.Series(None, index=df.index)
        tagged = feel.isin(FEEL_TO_ZONE)
        if tagged.any():
            # Keep the session's measured duration; only its *placement* changes.
            measured = chosen.sum(axis=1, min_count=1)
            dur = pd.to_numeric(df.get("duration_s"), errors="coerce")
            total = measured.where(measured.notna() & (measured > 0), dur)
            for i in range(1, 6):
                weights = feel.map(lambda f: FEEL_TO_ZONE.get(f, {}).get(i, 0.0))
                chosen.loc[tagged, i] = (total * weights)[tagged]
            src = np.where(tagged, "tagged", src)
        df["zone_source"] = src
    else:
        chosen, df["zone_source"] = hr_z, "hr"

    for i in range(1, 6):
        df[f"z{i}_s"] = chosen[i]
    zcols = [f"z{i}_s" for i in range(1, 6)]
    df["zone_total_s"] = df[zcols].sum(axis=1, min_count=1)
    for band in ("low", "moderate", "high"):
        members = [f"z{i}_s" for i, b in _BAND_OF_ZONE.items() if b == band]
        df[f"{band}_s"] = df[members].sum(axis=1, min_count=1)
    return df


def intensity_distribution(activities: pd.DataFrame, freq: str = "W-MON",
                           by_sport: bool = False, source: str = "hr",
                           tags: pd.DataFrame | None = None) -> pd.DataFrame:
    """Time-in-band per period (and optionally per sport).

    Returns: period_start[, sport], low_s, moderate_s, high_s, total_s,
    low_pct, moderate_pct, high_pct.
    """
    out_cols = ["period_start"] + (["sport"] if by_sport else []) + [
        "low_s", "moderate_s", "high_s", "total_s",
        "low_pct", "moderate_pct", "high_pct"]
    if not has_zone_data(activities, source):
        return pd.DataFrame(columns=out_cols)
    df = zone_time(activities, source=source, tags=tags)
    df = df[df["zone_total_s"] > 0].copy()
    if df.empty:
        return pd.DataFrame(columns=out_cols)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["period_start"] = df["date"].dt.to_period(
        "W-SUN" if freq.startswith("W") else freq).dt.start_time
    keys = ["period_start"] + (["sport"] if by_sport else [])
    g = df.groupby(keys).agg(
        low_s=("low_s", "sum"), moderate_s=("moderate_s", "sum"),
        high_s=("high_s", "sum"), total_s=("zone_total_s", "sum"),
    ).reset_index()
    for band in ("low", "moderate", "high"):
        g[f"{band}_pct"] = g[f"{band}_s"] / g["total_s"].replace(0, np.nan) * 100
    return g.sort_values(keys)


def _classify_tid(low: float, moderate: float, high: float) -> tuple[str, str]:
    """Name the shape of a low/moderate/high split (fractions), plus a status."""
    if high > low:
        return "Inverted", "critical"
    if low < 0.60:
        return "Threshold-heavy", "serious"
    if high >= moderate:
        return "Polarised", "good"
    return "Pyramidal", "good"


def intensity_summary(activities: pd.DataFrame, source: str = "hr",
                      tags: pd.DataFrame | None = None) -> dict | None:
    """Overall training-intensity distribution with a model name and verdict."""
    if not has_zone_data(activities, source):
        return None
    df = zone_time(activities, source=source, tags=tags)
    totals = {b: float(df[f"{b}_s"].sum(min_count=1) or 0) for b in
              ("low", "moderate", "high")}
    total = sum(totals.values())
    if total <= 0:
        return None
    shares = {b: v / total for b, v in totals.items()}
    label, status = _classify_tid(shares["low"], shares["moderate"], shares["high"])
    zones = {f"z{i}": float(df[f"z{i}_s"].sum(min_count=1) or 0) / total
             for i in range(1, 6)}
    used = df.loc[df["zone_total_s"] > 0, "zone_source"].value_counts().to_dict()
    return {
        "label": label, "status": status, "hours": total / 3600.0,
        "shares": shares, "zone_shares": zones,
        "source": source, "sessions_by_source": used,
        "score": round(intensity_score(shares["low"], shares["high"]), 1),
        "nearest_model": min(
            TID_MODELS,
            key=lambda m: sum(abs(TID_MODELS[m][i] - s) for i, s in
                              enumerate(shares[b] for b in ("low", "moderate", "high")))),
    }


def intensity_score(low_share: float, high_share: float) -> float:
    """0-100 for how well an intensity split matches the evidence-based models.

    Rewards a large easy base and penalises an oversized hard fraction. Both
    published models sit at 90-100 here; a split with more hard time than easy
    lands near the floor.
    """
    if not np.isfinite(low_share) or not np.isfinite(high_share):
        return np.nan
    s_low = np.interp(low_share, [0.30, 0.55, 0.75, 0.85], [0, 45, 90, 100])
    s_high = np.interp(high_share, [0.05, 0.20, 0.35, 0.50], [100, 90, 45, 0])
    return float(0.5 * s_low + 0.5 * s_high)


def rolling_metric(daily: pd.DataFrame, column: str, window: int = 7) -> pd.DataFrame:
    """Raw value plus a centered-ish rolling average for a daily metric.

    Returns columns: date, <column>, rolling.
    """
    if daily.empty or column not in daily.columns:
        return pd.DataFrame(columns=["date", column, "rolling"])
    df = daily[["date", column]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=[column]).sort_values("date")
    df["rolling"] = df[column].rolling(window, min_periods=1).mean()
    return df


def run_efficiency(activities: pd.DataFrame, aerobic_only: bool = False,
                   min_minutes: float = 20.0,
                   aerobic_low_share: float = 0.60) -> pd.DataFrame:
    """Aerobic efficiency for runs: pace per km vs. average HR over time.

    Lower pace at the same (or lower) HR over weeks = improving fitness.
    Returns: date, pace_min_per_km, avg_hr, efficiency (m per beat), aerobic.

    The efficiency factor is only a fitness signal when it is compared *like for
    like*. A tempo session and a recovery jog produce very different values for
    reasons that have nothing to do with fitness changing, so mixing every run
    into one trend measures session type, not adaptation. ``aerobic_only``
    restricts the sample to steady runs of at least ``min_minutes`` that spent
    at least ``aerobic_low_share`` of their time in HR zones 1-3.
    """
    cols = ["date", "pace_min_per_km", "avg_hr", "efficiency", "aerobic"]
    if activities.empty:
        return pd.DataFrame(columns=cols)
    df = activities[activities["sport"] == "run"].copy()
    df = df[(df["avg_speed_mps"] > 0) & (df["avg_hr"] > 0)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    # Metres per heartbeat is only meaningful if the heartbeat is real. A run
    # whose HR failed hr_quality would show a fake efficiency drop, so it is
    # dropped rather than trended.
    df = df[~hr_quality(df)["hr_suspect"]]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    df["pace_min_per_km"] = (1000.0 / df["avg_speed_mps"]) / 60.0
    # Distance covered per heartbeat — a simple efficiency factor.
    df["efficiency"] = df["avg_speed_mps"] * 60.0 / df["avg_hr"]

    z = zone_time(df)
    easy_share = (z["low_s"].fillna(0) + z["z3_s"].fillna(0)) / \
        z["zone_total_s"].replace(0, np.nan)
    long_enough = df["duration_s"].fillna(0) >= min_minutes * 60
    # With no zone data we can't tell sessions apart; treat them as unclassified
    # rather than silently assuming they qualify.
    df["aerobic"] = (easy_share >= aerobic_low_share).fillna(False) & long_enough

    if aerobic_only:
        df = df[df["aerobic"]]
    return df[cols].sort_values("date")


# ---------------------------------------------------------------------------
# Recovery: HRV against its own normal range
# ---------------------------------------------------------------------------
# HRV-guided training research does not act on the daily number — day-to-day
# noise swamps the signal. It compares a 7-day rolling mean of ln(HRV) against
# the athlete's own recent baseline, with a "normal range" of +/- 0.5 SD (the
# smallest worthwhile change). Inside the band = normal, below it = suppressed.

def hrv_baseline(daily: pd.DataFrame, roll: int = 7, base_window: int = 60,
                 swc_mult: float = 0.5) -> pd.DataFrame:
    """ln(HRV) 7-day mean with its personal normal range.

    Returns: date, hrv, ln_hrv, roll, baseline, swc, lower, upper, status, z.
    """
    cols = ["date", "hrv", "ln_hrv", "roll", "baseline", "swc",
            "lower", "upper", "status", "z"]
    if daily is None or daily.empty or "hrv_overnight" not in daily.columns:
        return pd.DataFrame(columns=cols)
    s = daily[["date", "hrv_overnight"]].dropna().copy()
    if s.empty:
        return pd.DataFrame(columns=cols)
    s["date"] = pd.to_datetime(s["date"]).dt.normalize()
    idx = pd.date_range(s["date"].min(), s["date"].max(), freq="D")
    hrv = s.groupby("date")["hrv_overnight"].mean().reindex(idx)

    out = pd.DataFrame({"date": idx, "hrv": hrv.values})
    # Log-transform: HRV is right-skewed, and the SWC convention is defined on
    # ln(rMSSD).
    out["ln_hrv"] = np.log(out["hrv"].where(out["hrv"] > 0))
    out["roll"] = out["ln_hrv"].rolling(roll, min_periods=max(2, roll // 2)).mean()
    out["baseline"] = out["roll"].rolling(base_window, min_periods=roll).mean()
    out["swc"] = out["ln_hrv"].rolling(base_window, min_periods=roll).std() * swc_mult
    out["lower"] = out["baseline"] - out["swc"]
    out["upper"] = out["baseline"] + out["swc"]
    out["z"] = (out["roll"] - out["baseline"]) / out["swc"].replace(0, np.nan)
    out["status"] = np.select(
        [out["roll"] > out["upper"], out["roll"] < out["lower"]],
        ["elevated", "suppressed"], default="normal")
    out.loc[out["roll"].isna() | out["baseline"].isna(), "status"] = "unknown"
    return out


# ---------------------------------------------------------------------------
# Monotony & strain (Foster)
# ---------------------------------------------------------------------------
# Same weekly load can be delivered as six identical days or as hard/easy
# alternation. Monotony = weekly mean daily load / SD of daily load; strain =
# weekly load x monotony. High monotony alongside high load is the combination
# associated with illness and non-functional overreaching.

def monotony_strain(activities: pd.DataFrame, through: pd.Timestamp | None = None,
                    window: int = 7) -> pd.DataFrame:
    """Rolling monotony and strain. Returns: date, load, weekly_load, monotony, strain."""
    dl = daily_load(activities, through=through)
    if dl.empty:
        return pd.DataFrame(columns=["date", "load", "weekly_load", "monotony", "strain"])
    dl = dl.set_index("date")
    mean = dl["load"].rolling(window, min_periods=window).mean()
    sd = dl["load"].rolling(window, min_periods=window).std()
    dl["weekly_load"] = dl["load"].rolling(window, min_periods=window).sum()
    # A week of identical days has zero variation, so the ratio is unbounded.
    # That is the *most* monotonous a week can be, not an unknown — pin it to
    # the cap rather than letting a divide-by-zero turn the worst case into a
    # blank.
    dl["monotony"] = (mean / sd).clip(upper=MONOTONY_CAP)
    dl.loc[(sd == 0) & (mean > 0), "monotony"] = MONOTONY_CAP
    # Monotony describes how a training week is *distributed*. A week with one
    # or two sessions has a distribution in name only, and produces a small
    # number that reads as "healthy variety" when it actually means "barely
    # trained". Below three training days the metric is not defined.
    train_days = (dl["load"] > 0).rolling(window, min_periods=window).sum()
    dl.loc[train_days < 3, "monotony"] = np.nan
    dl["strain"] = dl["weekly_load"] * dl["monotony"]
    return dl.reset_index()


# ---------------------------------------------------------------------------
# Discipline balance
# ---------------------------------------------------------------------------
# Ironman is three sports, and the classic failure mode of self-coached training
# is that the enjoyable one quietly eats the others. Typical time splits for
# long-course training sit near 20% swim / 50% bike / 30% run.

IRONMAN_TIME_SPLIT = {"swim": 0.20, "bike": 0.50, "run": 0.30}

# Days without a session before a discipline counts as neglected. Swim technique
# degrades fastest with time away, hence the tighter threshold.
_STALE_DAYS = {"swim": 7, "bike": 10, "run": 7}


def discipline_balance(activities: pd.DataFrame, window_days: int = 28,
                       through: pd.Timestamp | None = None) -> pd.DataFrame:
    """Per-discipline share of training time vs. target, and days since last session.

    Returns: sport, hours, share, target_share, gap_pct, sessions,
    last_date, days_since, stale.
    """
    cols = ["sport", "hours", "share", "target_share", "gap_pct",
            "sessions", "last_date", "days_since", "stale"]
    asof = pd.Timestamp(through).normalize() if through is not None else _today()
    if activities.empty:
        return pd.DataFrame(columns=cols)
    df = activities.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    recent = df[df["date"] > asof - pd.Timedelta(days=window_days)]

    rows = []
    total_h = float(recent["duration_s"].sum()) / 3600.0
    for sport, target in IRONMAN_TIME_SPLIT.items():
        sub = recent[recent["sport"] == sport]
        ever = df[df["sport"] == sport]
        hours = float(sub["duration_s"].sum()) / 3600.0
        last = ever["date"].max() if not ever.empty else pd.NaT
        days_since = int((asof - last).days) if pd.notna(last) else None
        share = (hours / total_h) if total_h > 0 else np.nan
        rows.append({
            "sport": sport, "hours": round(hours, 1),
            "share": share, "target_share": target,
            "gap_pct": (share - target) * 100 if np.isfinite(share) else np.nan,
            "sessions": int(len(sub)), "last_date": last,
            "days_since": days_since,
            "stale": days_since is None or days_since > _STALE_DAYS[sport],
        })
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Race-day projection
# ---------------------------------------------------------------------------

def race_projection(activities: pd.DataFrame, race_date, taper_days: int = 14,
                    taper_floor: float = 0.45,
                    through: pd.Timestamp | None = None) -> dict | None:
    """Project fitness and form to race day if current training continues.

    Assumes the average daily load of the last 28 days holds until the taper,
    then falls linearly to ``taper_floor`` of it on race day. Guidance for
    long-course racing is a 10-14 day taper, CTL peaking 2-3 weeks out and
    losing under ~10%, and race-day form (TSB) landing around +5 to +25.
    """
    if activities.empty or race_date is None:
        return None
    race = pd.Timestamp(race_date).normalize()
    asof = pd.Timestamp(through).normalize() if through is not None else _today()
    if race <= asof:
        return None

    ff = fitness_fatigue(activities, through=asof)
    if ff.empty:
        return None
    ctl = float(ff["ctl"].iloc[-1])
    atl = float(ff["atl"].iloc[-1])

    recent = daily_load(activities, through=asof).tail(28)
    typical = float(recent["load"].mean()) if not recent.empty else 0.0

    taper_start = race - pd.Timedelta(days=taper_days)
    days = pd.date_range(asof + pd.Timedelta(days=1), race, freq="D")
    path = []
    for d in days:
        if d < taper_start:
            load = typical
        else:
            frac = (race - d).days / max(1, taper_days)
            load = typical * (taper_floor + (1 - taper_floor) * frac)
        ctl += _ALPHA_CTL * (load - ctl)
        atl += _ALPHA_ATL * (load - atl)
        path.append({"date": d, "load": load, "ctl": ctl, "atl": atl,
                     "tsb": ctl - atl})

    tsb = ctl - atl
    if tsb < 0:
        verdict, status = "Under-tapered — still carrying fatigue", "serious"
    elif tsb <= 25:
        verdict, status = "On track for a fresh race day", "good"
    else:
        verdict, status = "Over-tapered — fitness left on the table", "warning"
    return {
        "race_date": race, "days_out": int((race - asof).days),
        "ctl_now": round(float(ff["ctl"].iloc[-1]), 1),
        "ctl_race": round(ctl, 1), "tsb_race": round(tsb, 1),
        "typical_daily_load": round(typical, 1),
        "taper_start": taper_start, "verdict": verdict, "status": status,
        "path": pd.DataFrame(path),
    }


def summary_stats(activities: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """Headline numbers for the dashboard top row."""
    out = {"total_sessions": 0, "total_hours": 0.0, "ctl": None,
           "tsb": None, "resting_hr": None, "vo2max_run": None}
    if not activities.empty:
        out["total_sessions"] = int(len(activities))
        out["total_hours"] = round(activities["duration_s"].sum() / 3600.0, 1)
        ff = fitness_fatigue(activities)
        if not ff.empty:
            out["ctl"] = round(float(ff["ctl"].iloc[-1]), 1)
            tsb = ff["tsb"].iloc[-1]
            out["tsb"] = round(float(tsb), 1) if pd.notna(tsb) else None
    if not daily.empty:
        rhr = daily["resting_hr"].dropna()
        if not rhr.empty:
            out["resting_hr"] = round(float(rhr.iloc[-1]), 0)
        vo2 = daily["vo2max_run"].dropna()
        if not vo2.empty:
            out["vo2max_run"] = round(float(vo2.iloc[-1]), 1)
    return out


# ---------------------------------------------------------------------------
# Progression Score
# ---------------------------------------------------------------------------
# A single 0-100 index of "am I improving?", built from four pillars that each
# reward *trends* (recent vs. personal baseline), not absolute values:
#
#   Fitness      - is chronic training load (CTL) ramping up?
#   Efficiency   - faster at the same HR (run efficiency factor) + resting HR falling?
#   Recovery     - HRV vs. baseline, sleep quality, and form (TSB) not too negative?
#   Consistency  - enough sessions per week, without long unplanned gaps?
#
# In "balanced" mode a poor Recovery pillar caps the whole score, so you can't
# score well by digging yourself into an overtraining hole.

# Pillar weights per philosophy. Renormalised over whichever pillars have data.
_WEIGHTS = {
    "balanced":    {"fitness": 0.25, "efficiency": 0.20, "recovery": 0.25,
                    "consistency": 0.15, "intensity": 0.15},
    "performance": {"fitness": 0.35, "efficiency": 0.25, "recovery": 0.10,
                    "consistency": 0.15, "intensity": 0.15},
    "health":      {"fitness": 0.15, "efficiency": 0.15, "recovery": 0.40,
                    "consistency": 0.15, "intensity": 0.15},
}

_PILLARS = ("fitness", "efficiency", "recovery", "consistency", "intensity")


def _wmean(pairs: list[tuple[pd.Series, float]], min_coverage: float = 0.5) -> pd.Series:
    """Row-wise weighted mean that ignores NaN inputs and renormalises.

    ``min_coverage`` is the fraction of total weight that must actually be
    present before a row gets a value. Without it a pillar silently collapses
    onto whichever input happens to be available — e.g. Recovery reducing to
    form (TSB) alone once HRV and sleep go stale, which then reports "recovery
    is strong" for someone who is simply not training.
    """
    idx = pairs[0][0].index
    num = pd.Series(0.0, index=idx)
    den = pd.Series(0.0, index=idx)
    total = sum(w for _, w in pairs)
    for s, w in pairs:
        s = s.astype("float64")
        mask = s.notna()
        num = num.add((s * w).where(mask, 0.0), fill_value=0.0)
        den = den.add(pd.Series(w, index=idx).where(mask, 0.0), fill_value=0.0)
    out = num / den.replace(0.0, np.nan)
    return out.where(den >= min_coverage * total)


def _days_since_activity(had_activity: pd.Series) -> pd.Series:
    """Days since the last activity, for each day in the index."""
    out, counter = [], None
    for v in had_activity.values:
        if v:
            counter = 0
        elif counter is not None:
            counter += 1
        out.append(counter if counter is not None else np.nan)
    return pd.Series(out, index=had_activity.index, dtype="float64")


def _daily_col(df: pd.DataFrame, col: str, idx: pd.DatetimeIndex) -> pd.Series:
    """A daily-indexed float series for a wellness/sleep column (NaN where absent)."""
    if df is None or df.empty or col not in df.columns:
        return pd.Series(np.nan, index=idx)
    s = df[["date", col]].dropna(subset=[col]).copy()
    if s.empty:
        return pd.Series(np.nan, index=idx)
    s["date"] = pd.to_datetime(s["date"]).dt.normalize()
    return s.groupby("date")[col].mean().reindex(idx)


def progression_series(
    activities: pd.DataFrame,
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    philosophy: str = "balanced",
    target_sessions: int = 6,
    strength: pd.DataFrame | None = None,
    through: pd.Timestamp | None = None,
    tags: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Daily 0-100 progression score plus its five pillar sub-scores.

    Columns: date, fitness, efficiency, recovery, consistency, intensity, score.
    The last row is the current headline score. ``strength`` (Que lift sessions,
    needs a ``date`` column) counts toward the Consistency pillar but is kept out
    of the endurance Fitness/Efficiency pillars.

    The series runs to ``through`` (default today), not to the last workout, so
    time off shows up as the decline it is.
    """
    cols = ["date", *_PILLARS, "score"]
    ff = fitness_fatigue(activities, through=through)
    if ff.empty:
        return pd.DataFrame(columns=cols)

    idx = pd.date_range(ff["date"].min(), ff["date"].max(), freq="D")
    ff = ff.set_index("date").reindex(idx)
    ctl, tsb, warm = ff["ctl"], ff["tsb"], ff["warmup"].fillna(False).astype(bool)

    # --- Fitness: CTL ramp per week (rising chronic load = building fitness) ---
    ramp_wk = ctl.diff(28) / 4.0
    fitness = (50 + ramp_wk * 10).clip(0, 100)  # +5 CTL/wk -> 100, -5/wk -> 0
    # During the warm-up window CTL climbs from its seed regardless of what the
    # athlete did, so a ramp read there is the filter's, not theirs. Scoring it
    # would open every new database on a flattering, meaningless high.
    fitness = fitness.where(~warm, np.nan)

    # --- Efficiency: run efficiency factor trend + resting-HR trend ---
    rhr = _daily_col(daily, "resting_hr", idx)
    rhr_recent = rhr.rolling(14, min_periods=3).mean()
    rhr_base = rhr.rolling(42, min_periods=7).mean().shift(14)
    rhr_base = rhr_base.fillna(rhr.expanding(min_periods=7).mean())
    score_rhr = (50 + (rhr_base - rhr_recent) * 8).clip(0, 100)  # -6 bpm -> ~100

    ef_series = pd.Series(np.nan, index=idx)
    # Aerobic runs only — see run_efficiency(). Comparing a threshold session to
    # last month's easy run measures the session, not the athlete.
    eff = run_efficiency(activities, aerobic_only=True)
    if not eff.empty:
        e = eff.copy()
        e["date"] = pd.to_datetime(e["date"]).dt.normalize()
        ef = e.groupby("date")["efficiency"].mean().reindex(idx).ffill()
        ef_recent = ef.rolling(21, min_periods=1).mean()
        ef_base = ef.rolling(56, min_periods=1).mean().shift(21)
        ef_base = ef_base.fillna(ef.expanding(min_periods=1).mean())
        ef_series = (50 + (ef_recent / ef_base - 1) * 800).clip(0, 100)  # +6% -> ~100
    efficiency = _wmean([(ef_series, 0.55), (score_rhr, 0.45)])

    # --- Recovery: HRV vs. its normal range + sleep quality + form (TSB) ---
    hb = hrv_baseline(daily)
    if hb.empty:
        score_hrv = pd.Series(np.nan, index=idx)
    else:
        # z is (7-day ln-HRV mean - baseline) / SWC. Inside +/-1 SWC is "normal",
        # which should read as a solid-but-not-outstanding 50-75.
        z = hb.set_index("date")["z"].reindex(idx)
        score_hrv = pd.Series(np.interp(z, [-3, -1, 0, 1, 3], [0, 30, 60, 80, 100]),
                              index=idx).where(z.notna())

    slp = _daily_col(sleep, "sleep_score", idx)
    sleep_recent = slp.rolling(7, min_periods=2).mean()
    score_sleep = ((sleep_recent - 40) / 50 * 100).clip(0, 100)  # 40 -> 0, 90 -> 100

    score_tsb = pd.Series(
        np.interp(tsb, [-30, -10, 0, 10], [0, 60, 90, 100]), index=idx
    ).where(tsb.notna())

    recovery = _wmean([(score_hrv, 0.40), (score_sleep, 0.35), (score_tsb, 0.25)])

    # --- Consistency: sessions/week vs. target, penalised for long gaps ---
    if activities.empty:
        cnt = pd.Series(0, index=idx)
    else:
        a = activities.copy()
        a["date"] = pd.to_datetime(a["date"]).dt.normalize()
        cnt = a.groupby("date").size().reindex(idx, fill_value=0)
    if strength is not None and not strength.empty:
        st = strength.copy()
        st["date"] = pd.to_datetime(st["date"]).dt.normalize()
        scnt = st.groupby("date").size().reindex(idx, fill_value=0)
        cnt = cnt.add(scnt, fill_value=0)
    sessions7 = cnt.rolling(7, min_periods=1).sum()
    score_freq = (sessions7 / target_sessions * 100).clip(0, 100)
    gap_penalty = (_days_since_activity(cnt > 0) - 2).clip(lower=0) * 12
    consistency = (score_freq - gap_penalty).clip(0, 100)

    # --- Intensity: is the easy/hard split anywhere near the models that work? ---
    # "auto" so a run with an untrustworthy HR trace is scored on power rather
    # than being counted as 80 minutes above threshold.
    intensity = pd.Series(np.nan, index=idx)
    if has_zone_data(activities, "auto"):
        z = zone_time(activities, source="auto", tags=tags)
        z["date"] = pd.to_datetime(z["date"]).dt.normalize()
        per_day = z.groupby("date")[["low_s", "moderate_s", "high_s"]].sum()
        per_day = per_day.reindex(idx, fill_value=0.0)
        # A 28-day window: long enough to cover a full easy/hard microcycle,
        # short enough to respond when the split changes.
        roll = per_day.rolling(28, min_periods=7).sum()
        tot = roll.sum(axis=1).replace(0, np.nan)
        intensity = pd.Series(
            [intensity_score(lo, hi) for lo, hi in
             zip(roll["low_s"] / tot, roll["high_s"] / tot)], index=idx)

    # --- Composite (weighted mean over available pillars) ---
    pillars = {"fitness": fitness, "efficiency": efficiency, "recovery": recovery,
               "consistency": consistency, "intensity": intensity}
    w = _WEIGHTS.get(philosophy, _WEIGHTS["balanced"])
    # Looser than the per-pillar threshold: a pillar collapsing onto a single
    # input is misleading, but a composite over two solid pillars is honest as
    # long as the caller can see which ones are missing.
    score = _wmean([(pillars[p], w[p]) for p in _PILLARS], min_coverage=0.35)
    # Balanced guardrail: recovery in the red caps the whole score.
    if philosophy != "performance":
        score = score.where(recovery >= 35, np.minimum(score, 55.0))

    return pd.DataFrame({
        "date": idx,
        **{p: pillars[p].values for p in _PILLARS},
        "score": score.values,
    })


# Status palette. Fixed semantics — these four colours only ever mean
# good / warning / serious / critical, and never stand in for a data series.
STATUS_COLORS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}


def _band(score: float) -> tuple[str, str]:
    """Verdict label + status colour for a 0-100 score."""
    if score >= 80:
        return "Thriving", STATUS_COLORS["good"]
    if score >= 65:
        return "Productive", STATUS_COLORS["good"]
    if score >= 50:
        return "Maintaining", STATUS_COLORS["warning"]
    if score >= 35:
        return "Stalling", STATUS_COLORS["serious"]
    return "Regressing", STATUS_COLORS["critical"]


_PILLAR_BLURB = {
    "fitness": ("fitness is climbing", "fitness has stalled", "fitness is slipping"),
    "efficiency": ("you're more efficient", "efficiency is flat", "efficiency is fading"),
    "recovery": ("recovery is strong", "recovery is so-so", "recovery is compromised"),
    "consistency": ("training is consistent", "consistency is patchy", "too many gaps"),
    "intensity": ("your easy/hard split is right", "the easy/hard split is drifting",
                  "too much of your training is hard"),
}


def progression_score(
    activities: pd.DataFrame,
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    philosophy: str = "balanced",
    target_sessions: int = 6,
    strength: pd.DataFrame | None = None,
    through: pd.Timestamp | None = None,
    tags: pd.DataFrame | None = None,
) -> dict | None:
    """Current headline progression score with pillar breakdown and a verdict.

    Returns None if there isn't enough data yet.
    """
    series = progression_series(activities, daily, sleep, philosophy,
                                target_sessions, strength, through=through,
                                tags=tags)
    valid = series.dropna(subset=["score"])
    if valid.empty:
        return None

    last = valid.iloc[-1]
    now = round(float(last["score"]), 1)  # band + display use the same rounded value
    # Trend vs. ~14 days ago.
    ref_date = last["date"] - pd.Timedelta(days=14)
    past = valid[valid["date"] <= ref_date]
    delta = round(now - float(past.iloc[-1]["score"]), 1) if not past.empty else None

    pillars = {p: (float(last[p]) if pd.notna(last[p]) else None) for p in _PILLARS}
    label, color = _band(now)

    # Plain-English drivers: best and worst populated pillar.
    present = {p: v for p, v in pillars.items() if v is not None}
    best = max(present, key=present.get)
    worst = min(present, key=present.get)

    def _blurb(p: str) -> str:
        good, mid, bad = _PILLAR_BLURB[p]
        v = present[p]
        return good if v >= 65 else (mid if v >= 50 else bad)

    if present[best] >= 50:
        verdict = f"{label} — {_blurb(best)}"
        if worst != best and present[worst] < 50:
            verdict += f", but {_blurb(worst)}"
    else:
        # Nothing is going well; leading with the "best" pillar would put a
        # negative phrase where the reader expects the good news.
        verdict = f"{label} — {_blurb(worst)}"
        rest = [p for p in present if p != worst]
        if rest:
            verdict += f", and {_blurb(min(rest, key=present.get))}"

    return {
        "score": now,
        "delta": delta,
        "label": label,
        "color": color,
        "verdict": verdict,
        "pillars": pillars,
        "missing": [p for p in _PILLARS if pillars[p] is None],
        "best": best,
        "worst": worst,
        "series": series,
    }


# ---------------------------------------------------------------------------
# Actionable flags
# ---------------------------------------------------------------------------
# A dashboard that only draws curves makes you do the interpreting. These are the
# handful of conditions worth interrupting for, each with the number behind it.

_SEVERITY_ORDER = {"critical": 0, "serious": 1, "warning": 2, "good": 3}


def training_flags(activities: pd.DataFrame, daily: pd.DataFrame,
                   sleep: pd.DataFrame, through: pd.Timestamp | None = None,
                   stale_sync_days: int = 3,
                   tags: pd.DataFrame | None = None) -> list[dict]:
    """Ranked list of {status, title, detail} worth acting on."""
    asof = pd.Timestamp(through).normalize() if through is not None else _today()
    flags: list[dict] = []

    def add(status, title, detail):
        flags.append({"status": status, "title": title, "detail": detail})

    # --- Is the data even current? Every number below inherits this. ---
    last_any = pd.NaT
    for df in (activities, daily):
        if df is not None and not df.empty and "date" in df:
            d = pd.to_datetime(df["date"]).max()
            last_any = d if pd.isna(last_any) else max(last_any, d)
    if pd.notna(last_any):
        stale = int((asof - last_any).days)
        if stale > stale_sync_days:
            # Everything else on the page is computed as of today against this
            # data, so a stale database is the first thing to know about.
            add("critical" if stale > 7 else "serious", f"Data is {stale} days old",
                f"Last record is {last_any:%d %b}. Run `python track.py sync` — "
                "everything below is computed as of today, so a stale database "
                "reads as time off.")

    # --- Detraining / ramping too hard ---
    ff = fitness_fatigue(activities, through=asof)
    rr = ramp_rate(ff)
    if rr:
        if rr["status"] == "serious":
            add("serious", f"Fitness falling {abs(rr['per_week']):.1f}/week",
                f"CTL is down to {rr['ctl']:.0f}. Detraining is fastest in the "
                "first two weeks off — a couple of easy sessions arrests most of it.")
        elif rr["status"] == "critical":
            add("warning", f"Ramping {rr['per_week']:.1f} CTL/week",
                "Sustained growth above ~5-7/week is the range where injury and "
                "illness risk climbs. Consider an easier week.")

    # --- Heart-rate sensor quality ---
    # Ranked high on purpose: if HR is wrong, the intensity distribution, the
    # efficiency pillar and the zone times below are all wrong with it.
    hq = hr_quality_summary(activities, tags=tags)
    if hq and hq["share"] >= 0.3:
        detail = (f"{hq['suspect']} of {hq['runs']} runs have a heart-rate trace "
                  "that doesn't hold up")
        if hq["hr_hard_median"] is not None and hq["pwr_hard_median"] is not None:
            detail += (f" — HR says {hq['hr_hard_median']:.0f}% of run time was "
                       f"above threshold, power says {hq['pwr_hard_median']:.0f}%")
        detail += (". Wrist sensors lock onto foot-strike cadence while running. "
                   "These runs are scored from power instead — or tag one by "
                   "hand and your own read of it wins outright.")
        if hq.get("tagged"):
            detail += f" ({hq['tagged']} already tagged.)"
        add("serious", "Run heart rate is unreliable", detail)

    # --- Intensity distribution ---
    tid = intensity_summary(activities, source="auto", tags=tags)
    if tid and tid["status"] in ("critical", "serious"):
        s = tid["shares"]
        add(tid["status"], f"{tid['label']} intensity distribution",
            f"{s['low']*100:.0f}% easy / {s['moderate']*100:.0f}% moderate / "
            f"{s['high']*100:.0f}% hard. Both models that hold up in the "
            "literature put ~75-80% of time easy; you're at "
            f"{s['low']*100:.0f}%.")

    # --- Neglected disciplines ---
    bal = discipline_balance(activities, through=asof)
    if not bal.empty:
        for _, r in bal[bal["stale"]].iterrows():
            if r["days_since"] is None:
                add("serious", f"No {r['sport']} sessions on record",
                    f"Target is ~{r['target_share']*100:.0f}% of training time.")
            else:
                add("warning", f"{r['days_since']} days since your last {r['sport']}",
                    f"{r['hours']:.1f}h in the last 4 weeks "
                    f"({r['share']*100:.0f}% of time vs a "
                    f"~{r['target_share']*100:.0f}% target).")

    # --- HRV suppressed against its own normal range ---
    hb = hrv_baseline(daily)
    if not hb.empty:
        cur = hb.dropna(subset=["z"])
        if not cur.empty and cur.iloc[-1]["status"] == "suppressed":
            add("serious", "HRV below your normal range",
                f"7-day average is {abs(cur.iloc[-1]['z']):.1f} SWC below "
                "baseline. The HRV-guided protocols swap intensity for easy "
                "aerobic work until it returns to the band.")

    # --- Monotony ---
    ms = monotony_strain(activities, through=asof)
    if not ms.empty:
        cur = ms.dropna(subset=["monotony"])
        if not cur.empty and float(cur.iloc[-1]["monotony"]) >= 2.0 and \
                float(cur.iloc[-1]["weekly_load"]) > 0:
            add("warning", f"Training monotony {cur.iloc[-1]['monotony']:.1f}",
                "Every day looking the same, at volume, is the combination "
                "Foster linked to illness and overreaching. Make easy days easier "
                "and hard days harder.")

    flags.sort(key=lambda f: _SEVERITY_ORDER.get(f["status"], 9))
    return flags


# ---------------------------------------------------------------------------
# Strength progression (from the Que workout app)
# ---------------------------------------------------------------------------
# The headline question is simply: gaining, maintaining, or losing strength?
# We answer it from estimated 1-rep-max (e1RM) trends per exercise, then roll
# those up into one verdict. Weights are in pounds (Que's unit).

_STRENGTH_THRESHOLD = 2.0  # % change that separates gaining / maintaining / losing


def _strength_verdict(delta_pct: float, thr: float = _STRENGTH_THRESHOLD) -> tuple[str, str]:
    if delta_pct > thr:
        return "Gaining", "#16a34a"
    if delta_pct < -thr:
        return "Losing", "#ef4444"
    return "Maintaining", "#eab308"


def strength_best_e1rm(sets: pd.DataFrame) -> pd.DataFrame:
    """Best estimated 1-rep max per exercise per session day.

    Returns columns: date, exercise, best_e1rm.
    """
    if sets is None or sets.empty:
        return pd.DataFrame(columns=["date", "exercise", "best_e1rm"])
    df = sets.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    g = (df.groupby(["date", "exercise"])["e1rm_lb"].max()
         .reset_index().rename(columns={"e1rm_lb": "best_e1rm"}))
    return g.sort_values("date")


def strength_trend(sets: pd.DataFrame, recent_days: int = 28,
                   min_sessions: int = 2) -> dict | None:
    """Overall gaining/maintaining/losing verdict + per-exercise breakdown.

    Compares each exercise's recent best e1RM (last ``recent_days``) against its
    earlier baseline, then averages the % changes (weighted by how often the
    exercise was trained).
    """
    best = strength_best_e1rm(sets)
    if best.empty:
        return None
    asof = best["date"].max()
    recent_cut = asof - pd.Timedelta(days=recent_days)

    per = []
    for ex, grp in best.groupby("exercise"):
        grp = grp.sort_values("date")
        if grp["date"].nunique() < min_sessions:
            continue
        recent = grp[grp["date"] > recent_cut]["best_e1rm"]
        base = grp[grp["date"] <= recent_cut]["best_e1rm"]
        if recent.empty:
            recent = grp["best_e1rm"].tail(1)
        if base.empty:  # no history before the recent window — use the earliest half
            base = grp["best_e1rm"].head(max(1, len(grp) // 2))
        r, b = float(recent.mean()), float(base.mean())
        if b <= 0:
            continue
        delta = (r / b - 1) * 100
        label, _ = _strength_verdict(delta)
        per.append({
            "exercise": ex,
            "sessions": int(grp["date"].nunique()),
            "recent_e1rm": round(r, 1),
            "baseline_e1rm": round(b, 1),
            "latest_e1rm": round(float(grp["best_e1rm"].iloc[-1]), 1),
            "delta_pct": round(delta, 1),
            "trend": label,
        })
    if not per:
        return None

    wsum = sum(p["sessions"] for p in per)
    overall = sum(p["delta_pct"] * p["sessions"] for p in per) / wsum
    verdict, color = _strength_verdict(overall)
    return {
        "verdict": verdict,
        "color": color,
        "overall_pct": round(overall, 1),
        "n_lifts": len(per),
        "per_exercise": sorted(per, key=lambda p: -p["sessions"]),
        "asof": asof,
    }


def strength_index(sets: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
    """A single normalised strength trend line (100 = your starting strength).

    Each exercise's best e1RM is indexed to its own first sessions, then averaged
    across exercises per period. Rising line = getting stronger overall.
    Returns columns: date, index.
    """
    best = strength_best_e1rm(sets)
    if best.empty:
        return pd.DataFrame(columns=["date", "index"])
    parts = []
    for ex, grp in best.groupby("exercise"):
        grp = grp.sort_values("date")
        base = float(grp["best_e1rm"].head(2).mean())
        if base <= 0:
            continue
        g = grp[["date"]].copy()
        g["index"] = grp["best_e1rm"].values / base * 100.0
        parts.append(g)
    if not parts:
        return pd.DataFrame(columns=["date", "index"])
    allg = pd.concat(parts).set_index("date")
    weekly = allg.groupby(pd.Grouper(freq=freq))["index"].mean().dropna().reset_index()
    return weekly


def strength_weekly(sessions: pd.DataFrame) -> pd.DataFrame:
    """Weekly tonnage, session count and total sets from lift sessions.

    Returns columns: week_start, tonnage_lb, sessions, sets.
    """
    if sessions is None or sessions.empty:
        return pd.DataFrame(columns=["week_start", "tonnage_lb", "sessions", "sets"])
    df = sessions.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["week_start"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.normalize()
    g = df.groupby("week_start").agg(
        tonnage_lb=("tonnage_lb", "sum"),
        sessions=("session_id", "count"),
        sets=("n_sets", "sum"),
    ).reset_index()
    return g.sort_values("week_start")
