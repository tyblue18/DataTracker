"""Export a real-data snapshot for design work.

Design tools and coding agents lay out much better against real numbers than
against placeholders — real data has awkward gaps, unavailable pillars and
lopsided distributions that placeholder data never has, and those are exactly
the states a design has to handle.

Regenerate with ``python track.py handoff`` after any metrics change, so the
brief and the sample never drift apart from the app.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import db, metrics
from .config import settings

OUT_DIR = Path(__file__).resolve().parent.parent / "design_handoff"


def _clean(obj):
    """Make anything pandas/numpy hands us JSON-safe."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, pd.DataFrame):
        return _clean(obj.to_dict("records"))
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(obj).strftime("%Y-%m-%d")
    if obj is None or obj is pd.NaT:
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if not np.isfinite(obj) else round(float(obj), 3)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


def _tail(df: pd.DataFrame, cols: list[str], n: int) -> list[dict]:
    if df is None or df.empty:
        return []
    keep = [c for c in cols if c in df.columns]
    return _clean(df[keep].tail(n))


def build_snapshot(through: pd.Timestamp | None = None) -> dict:
    """Everything a designer needs to lay out every panel, with real values."""
    db.init_db()
    acts = db.load_activities()
    daily = db.load_daily()
    sleep = db.load_sleep()
    strength = db.load_strength_sessions()
    asof = pd.Timestamp(through).normalize() if through is not None \
        else pd.Timestamp(date.today())

    prog = metrics.progression_score(
        acts, daily, sleep, target_sessions=settings.weekly_session_target,
        strength=strength, through=asof)
    ff = metrics.fitness_fatigue(acts, through=asof)
    tags = db.load_session_tags()
    tid = metrics.intensity_summary(acts, source="auto")
    by_sport = metrics.intensity_distribution(acts, by_sport=True, source="auto")
    weekly_tid = metrics.intensity_distribution(acts, source="auto")
    hb = metrics.hrv_baseline(daily)
    bal = metrics.discipline_balance(acts, through=asof)
    eff = metrics.run_efficiency(acts)

    last_data = max([pd.to_datetime(df["date"]).max()
                     for df in (acts, daily) if not df.empty], default=pd.NaT)

    snapshot = {
        "meta": {
            "generated": asof.strftime("%Y-%m-%d"),
            "note": "Real data from the athlete's database. Regenerate with "
                    "`python track.py handoff`.",
            "data_span": [_clean(pd.to_datetime(acts["date"]).min()) if not acts.empty else None,
                          _clean(last_data)],
            "days_stale": int((asof - last_data).days) if pd.notna(last_data) else None,
            "counts": {"activities": int(len(acts)), "wellness_days": int(len(daily)),
                       "sleep_nights": int(len(sleep)),
                       "strength_sessions": int(len(strength))},
            "race_date": settings.race_date,
            "race_name": settings.race_name,
        },

        # The alert strip. Design this: it is the first thing on the page.
        "flags": _clean(metrics.training_flags(acts, daily, sleep, through=asof)),

        # Hero. `pillars` can contain nulls — that state needs a design.
        "progression_score": _clean({
            k: v for k, v in (prog or {}).items() if k != "series"
        }) if prog else None,
        "progression_series": _tail(
            prog["series"], ["date", *metrics._PILLARS, "score"], 90) if prog else [],

        # Training load.
        "fitness_fatigue": _tail(ff, ["date", "load", "ctl", "atl", "tsb", "warmup"], 90),
        "ramp_rate": _clean(metrics.ramp_rate(ff)),
        "monotony": _tail(metrics.monotony_strain(acts, through=asof),
                          ["date", "weekly_load", "monotony", "strain"], 60),

        # Sensor trust. Drives a panel of its own, and explains why some
        # sessions are scored from power rather than heart rate.
        "hr_quality": _clean(metrics.hr_quality_summary(acts)),
        "intensity_by_source": {
            src: _clean(metrics.intensity_summary(acts, source=src))
            for src in ("hr", "power", "auto")
        },
        "session_tags": {
            "coverage": _clean(metrics.tag_coverage(acts, tags)),
            "feel_vs_zones": _tail(metrics.feel_vs_zones(acts, tags),
                                   ["date", "sport", "feel", "rpe", "zone_source",
                                    "low_pct", "high_pct", "sensor_band", "agrees"], 30),
        },

        # Intensity distribution.
        "intensity_summary": _clean(tid),
        "intensity_models": {k: {"low": v[0], "moderate": v[1], "high": v[2]}
                             for k, v in metrics.TID_MODELS.items()},
        "intensity_by_sport": _clean(by_sport),
        "intensity_weekly": _tail(
            weekly_tid, ["period_start", "low_s", "moderate_s", "high_s",
                         "total_s", "low_pct", "moderate_pct", "high_pct"], 20),

        # Recovery.
        "hrv_baseline": _tail(
            hb, ["date", "hrv", "ln_hrv", "roll", "baseline", "lower", "upper",
                 "status", "z"], 90),
        "resting_hr": _tail(metrics.rolling_metric(daily, "resting_hr"),
                            ["date", "resting_hr", "rolling"], 90),
        "sleep": _tail(sleep, ["date", "total_sleep_s", "deep_s", "rem_s",
                               "sleep_score"], 60),
        "run_efficiency": _clean(eff),

        # Disciplines & volume.
        "discipline_balance": _clean(bal),
        "discipline_target": metrics.IRONMAN_TIME_SPLIT,
        "weekly_volume": _tail(metrics.weekly_volume(acts),
                               ["week_start", "sport", "distance_km", "hours",
                                "training_load", "sessions"], 60),
        "activities_sample": _tail(
            acts, ["date", "sport", "distance_m", "duration_s", "avg_hr",
                   "training_load", "hr_z1_s", "hr_z2_s", "hr_z3_s",
                   "hr_z4_s", "hr_z5_s"], 12),

        "race_projection": _clean({
            k: v for k, v in (metrics.race_projection(
                acts, settings.race_date, through=asof) or {}).items()
            if k != "path"
        }) or None,
    }
    return snapshot


def write_snapshot(path: Path | None = None) -> Path:
    path = path or (OUT_DIR / "sample_data.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_snapshot(), indent=2), encoding="utf-8")
    return path
