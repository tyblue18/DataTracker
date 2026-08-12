"""Pull data from Garmin Connect into the local database.

Everything is parsed defensively with ``.get()`` because Garmin's payload
shapes drift between account types and firmware versions. Anything we can't
map is still preserved in the ``raw`` JSON column, so no data is lost.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import db, metrics
from .client import connect

# Map Garmin's granular activity typeKeys onto the three Ironman disciplines.
SPORT_MAP = {
    "running": "run",
    "treadmill_running": "run",
    "trail_running": "run",
    "track_running": "run",
    "indoor_running": "run",
    "obstacle_run": "run",
    "cycling": "bike",
    "road_biking": "bike",
    "indoor_cycling": "bike",
    "mountain_biking": "bike",
    "gravel_cycling": "bike",
    "virtual_ride": "bike",
    "cyclocross": "bike",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "swimming": "swim",
}


def _sport(type_key: str | None) -> str:
    if not type_key:
        return "other"
    return SPORT_MAP.get(type_key, "other")


def _num(v):
    """Coerce to float or None."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #

def _pool_length_m(a: dict) -> float | None:
    """Pool length in metres. Garmin stores it in the unit's own base units."""
    length = _num(a.get("poolLength"))
    if length is None:
        return None
    unit = a.get("unitOfPoolLength") or {}
    factor = _num(unit.get("factor")) or 1.0
    metres = length / factor if factor else length
    # Yards come through as a yard-based unit; normalise to metres.
    if str(unit.get("unitKey", "")).startswith("yard"):
        metres *= 0.9144
    return round(metres, 2)


def parse_activity(a: dict) -> dict:
    type_key = (a.get("activityType") or {}).get("typeKey")
    start_local = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
    day = start_local.split(" ")[0] if start_local else ""
    # Cadence is reported under a sport-specific key.
    cadence = (a.get("averageRunningCadenceInStepsPerMinute")
               or a.get("averageBikingCadenceInRevsPerMinute")
               or a.get("averageSwimCadenceInStrokesPerMinute"))
    row = {
        "activity_id": a.get("activityId"),
        "date": day,
        "start_time": start_local,
        "sport": _sport(type_key),
        "sub_sport": type_key,
        "name": a.get("activityName"),
        "distance_m": _num(a.get("distance")),
        "duration_s": _num(a.get("duration")),
        "elapsed_s": _num(a.get("elapsedDuration")),
        "moving_s": _num(a.get("movingDuration")),
        "avg_hr": _num(a.get("averageHR")),
        "max_hr": _num(a.get("maxHR")),
        "avg_speed_mps": _num(a.get("averageSpeed")),
        "elevation_gain_m": _num(a.get("elevationGain")),
        "calories": _num(a.get("calories")),
        "avg_power_w": _num(a.get("avgPower") or a.get("averagePower")),
        "norm_power_w": _num(a.get("normPower")),
        "training_load": _num(a.get("activityTrainingLoad")),
        "aerobic_te": _num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": _num(a.get("anaerobicTrainingEffect")),
        "intensity_min_mod": _num(a.get("moderateIntensityMinutes")),
        "intensity_min_vig": _num(a.get("vigorousIntensityMinutes")),
        "vo2max_activity": _num(a.get("vO2MaxValue")),
        "avg_cadence": _num(cadence),
        "avg_stride_m": (_num(a.get("avgStrideLength")) or 0) / 100.0 or None,  # cm -> m
        "avg_gct_ms": _num(a.get("avgGroundContactTime")),
        "avg_vert_osc_cm": _num(a.get("avgVerticalOscillation")),
        "avg_vert_ratio": _num(a.get("avgVerticalRatio")),
        "avg_swolf": _num(a.get("averageSwolf")),
        "pool_length_m": _pool_length_m(a),
        "raw": a,
    }
    # Time-in-zone: the raw payload already carries this for every activity, and
    # it is what makes intensity distribution computable.
    for i in range(1, 6):
        row[f"hr_z{i}_s"] = _num(a.get(f"hrTimeInZone_{i}"))
        row[f"pwr_z{i}_s"] = _num(a.get(f"powerTimeInZone_{i}"))
    return row


def sync_activities(garmin, start: date, end: date, conn) -> dict:
    """Upsert every activity in the window; report how many were actually new.

    The window count on its own is misleading feedback for a sync button: it
    reports how many sessions the last N days contain, which barely moves, so a
    sync that fetched nothing new looks identical to one that fetched a race.
    Checking which ids were already stored costs one query and makes the
    difference visible.
    """
    activities = garmin.get_activities_by_date(
        start.isoformat(), end.isoformat()
    )
    rows = [r for r in (parse_activity(a) for a in activities or [])
            if r["activity_id"] is not None]

    known: set = set()
    if rows:
        ids = [r["activity_id"] for r in rows]
        marks = ", ".join(["?"] * len(ids))
        known = {r[0] for r in conn.execute(
            db._ph(f"SELECT activity_id FROM activities "
                   f"WHERE activity_id IN ({marks})"), ids).fetchall()}

    for row in rows:
        db.upsert_activity(conn, row)
    return {"seen": len(rows),
            "new": sum(1 for r in rows if r["activity_id"] not in known)}


# --------------------------------------------------------------------------- #
# Per-activity streams -> aerobic decoupling
# --------------------------------------------------------------------------- #
# The activity summary carries no time series, so aerobic decoupling (how far
# output-to-HR drifts across a long session) needs the detail payload — one extra
# request per activity. Garmin returns it as a column store: a list of metric
# descriptors naming each column, and a row per sample. We pull the columns we
# need by name and hand them to metrics.aerobic_decoupling.

# Garmin's descriptor keys for the streams we care about.
_STREAM_KEYS = {"hr": "directHeartRate", "power": "directPower",
                "speed": "directSpeed", "time": "sumElapsedDuration"}


def extract_streams(details: dict) -> dict:
    """Pull aligned hr / power / speed / time series from a details payload.

    Returns a dict of lists (missing streams come back empty). Defensive by
    design — descriptor order and which streams exist both vary by device.
    """
    d = details or {}
    idx = {}
    for desc in d.get("metricDescriptors") or []:
        if isinstance(desc, dict) and desc.get("key") is not None:
            idx[desc["key"]] = desc.get("metricsIndex")
    samples = d.get("activityDetailMetrics") or []

    def series(key: str) -> list:
        i = idx.get(key)
        if i is None:
            return []
        out = []
        for s in samples:
            m = s.get("metrics") if isinstance(s, dict) else None
            out.append(m[i] if (m is not None and i < len(m)) else None)
        return out

    streams = {name: series(k) for name, k in _STREAM_KEYS.items()}
    if not streams["time"]:  # older payloads name it differently
        streams["time"] = series("directTimestamp")
    return streams


def _usable(series: list, minimum: int = 20) -> bool:
    return sum(1 for v in series if isinstance(v, (int, float)) and v and v > 0) >= minimum


def decoupling_from_details(details: dict, sport: str) -> float | None:
    """Aerobic decoupling for a run/bike detail payload, or None.

    Prefers power (a direct output measure, immune to GPS pacing noise) and falls
    back to speed. Swims and untyped activities are skipped — decoupling is a
    steady-endurance measure and foot/pedal output is what it reads.
    """
    if sport not in ("run", "bike"):
        return None
    s = extract_streams(details)
    if not s["hr"]:
        return None
    if _usable(s["power"]):
        output = s["power"]
    elif _usable(s["speed"]):
        output = s["speed"]
    else:
        return None
    return metrics.aerobic_decoupling(output, s["hr"], times=s["time"] or None)


# --------------------------------------------------------------------------- #
# Daily wellness
# --------------------------------------------------------------------------- #

def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def parse_daily(cdate: str, stats, hrv, vo2, training) -> dict:
    stats = stats or {}

    hrv_avg = hrv_status = None
    if isinstance(hrv, dict):
        summ = hrv.get("hrvSummary") or {}
        hrv_avg = _num(summ.get("lastNightAvg"))
        hrv_status = summ.get("status")

    vo2_run = vo2_bike = None
    if isinstance(vo2, list) and vo2:
        m = vo2[0] or {}
        generic = m.get("generic") or {}
        cycling = m.get("cycling") or {}
        vo2_run = _num(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"))
        vo2_bike = _num(cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"))

    train_status = None
    if isinstance(training, dict):
        latest = training.get("mostRecentTrainingStatus") or {}
        details = latest.get("latestTrainingStatusData") or {}
        # value is keyed by device id; grab the first entry's trainingStatus
        for v in details.values():
            if isinstance(v, dict):
                train_status = v.get("trainingStatusFeedbackPhrase") or v.get("trainingStatus")
                break

    return {
        "date": cdate,
        "resting_hr": _num(stats.get("restingHeartRate")),
        "hrv_overnight": hrv_avg,
        "hrv_status": hrv_status,
        "stress_avg": _num(stats.get("averageStressLevel")),
        "body_battery_high": _num(stats.get("bodyBatteryHighestValue")),
        "body_battery_low": _num(stats.get("bodyBatteryLowestValue")),
        "steps": _num(stats.get("totalSteps")),
        "vo2max_run": vo2_run,
        "vo2max_bike": vo2_bike,
        "training_status": train_status,
        # Garmin reports weight in grams here, and omits it entirely on most
        # days unless a smart scale is synced.
        "weight_kg": (weight_g / 1000.0) if (weight_g := _num(stats.get("weight"))) else None,
        "raw": {"stats": stats},
    }


def parse_sleep(cdate: str, sleep) -> dict | None:
    if not isinstance(sleep, dict):
        return None
    dto = sleep.get("dailySleepDTO") or {}
    if not dto:
        return None
    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value")
    return {
        "date": cdate,
        "total_sleep_s": _num(dto.get("sleepTimeSeconds")),
        "deep_s": _num(dto.get("deepSleepSeconds")),
        "light_s": _num(dto.get("lightSleepSeconds")),
        "rem_s": _num(dto.get("remSleepSeconds")),
        "awake_s": _num(dto.get("awakeSleepSeconds")),
        "sleep_score": _num(overall),
        "raw": {"dailySleepDTO": dto},
    }


def _settled_days(conn, start: date, end: date, recheck_days: int) -> set[str]:
    """Dates whose wellness+sleep rows are already filled in and unlikely to change.

    Garmin backfills recent days (sleep scores and HRV land hours late), so the
    most recent ``recheck_days`` are always re-fetched regardless.
    """
    cutoff = (end - timedelta(days=recheck_days - 1)).isoformat()
    rows = conn.execute(
        db._ph(
            "SELECT d.date FROM daily_metrics d JOIN sleep s ON s.date = d.date "
            "WHERE d.date BETWEEN ? AND ? AND d.date < ? "
            "  AND d.resting_hr IS NOT NULL AND s.total_sleep_s IS NOT NULL"),
        (start.isoformat(), end.isoformat(), cutoff),
    ).fetchall()
    return {r[0] for r in rows}


def sync_wellness(garmin, start: date, end: date, conn, *, full: bool = False,
                  recheck_days: int = 3, progress=None) -> int:
    """Sync per-day wellness. Five API calls per day, so days already stored are
    skipped unless ``full`` is set — a 90-day re-sync goes from 450 requests to
    roughly ``recheck_days * 5``.
    """
    done = set() if full else _settled_days(conn, start, end, recheck_days)
    count = 0
    for d in _daterange(start, end):
        cdate = d.isoformat()
        if cdate in done:
            continue
        if progress:
            progress(cdate)
        stats = _safe(garmin.get_stats, cdate)
        hrv = _safe(garmin.get_hrv_data, cdate)
        vo2 = _safe(garmin.get_max_metrics, cdate)
        training = _safe(garmin.get_training_status, cdate)
        sleep = _safe(garmin.get_sleep_data, cdate)

        daily = parse_daily(cdate, stats, hrv, vo2, training)
        db.upsert_daily(conn, daily)

        sleep_row = parse_sleep(cdate, sleep)
        if sleep_row:
            db.upsert_sleep(conn, sleep_row)
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Backfill — re-parse what is already stored, no network
# --------------------------------------------------------------------------- #

def backfill_from_raw() -> dict:
    """Re-parse the stored ``raw`` JSON of every activity through the current
    parser.

    Activities synced by an earlier version kept the whole Garmin payload but
    only mapped a subset of it into columns. This replays the payload through
    ``parse_activity`` so new columns (time-in-zone, running dynamics, …) get
    filled in without a single Garmin request.
    """
    db.init_db()
    updated = skipped = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT activity_id, raw FROM activities").fetchall()
        for aid, raw in rows:
            if not raw:
                skipped += 1
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                skipped += 1
                continue
            parsed = parse_activity(payload)
            if parsed.get("activity_id") is None:
                parsed["activity_id"] = aid
            db.upsert_activity(conn, parsed)
            updated += 1
        db.log_sync(conn, "backfill", f"{updated} activities re-parsed",
                    datetime.now().isoformat(timespec="seconds"))
    return {"updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

class MFARequired(RuntimeError):
    """Garmin wants a 2FA code but there is nobody to type one.

    Raised instead of blocking on stdin, so a button in a UI (or a scheduled
    run) fails with something actionable rather than hanging forever.
    """


def _no_prompt(_msg: str = "") -> str:
    raise MFARequired(
        "Garmin needs a two-factor code and this run can't ask for one. "
        "Run `python track.py sync` once in a terminal to complete the login — "
        "the cached tokens are then reused and refreshed automatically.")


def run_sync(days: int = 30, activities_only: bool = False,
             wellness_only: bool = False, mfa_prompt=input,
             full: bool = False, progress=None) -> dict:
    """Sync the last ``days`` days of data into the database.

    By default only days that aren't already stored are fetched; pass
    ``full=True`` to re-fetch the whole window. Returns a summary dict.
    """
    db.init_db()
    garmin = connect(mfa_prompt=mfa_prompt)

    end = date.today()
    start = end - timedelta(days=max(0, days - 1))

    summary = {"start": start.isoformat(), "end": end.isoformat(),
               "days": days, "activities": 0, "new_activities": 0,
               "wellness_days": 0}

    with db.connect() as conn:
        if not wellness_only:
            counts = sync_activities(garmin, start, end, conn)
            summary["activities"] = counts["seen"]
            summary["new_activities"] = counts["new"]
        if not activities_only:
            summary["wellness_days"] = sync_wellness(
                garmin, start, end, conn, full=full, progress=progress)
        db.log_sync(
            conn, "sync",
            f"{summary['activities']} activities "
            f"({summary['new_activities']} new), "
            f"{summary['wellness_days']} wellness days "
            f"({start} -> {end})",
            datetime.now().isoformat(timespec="seconds"),
        )
    return summary


def sync_activity_details(garmin, conn, min_minutes: int = 45,
                          limit: int | None = None, progress=None) -> dict:
    """Fetch streams for long run/bike sessions and store their decoupling.

    Only sessions at least ``min_minutes`` long that don't already have a
    decoupling value are fetched — one request each, newest first — so re-running
    is cheap and resumable. ``limit`` caps how many to pull in one run.
    """
    rows = conn.execute(
        db._ph(
            "SELECT activity_id, sport FROM activities "
            "WHERE sport IN ('run', 'bike') AND duration_s >= ? "
            "AND decoupling_pct IS NULL ORDER BY date DESC"),
        (min_minutes * 60.0,),
    ).fetchall()
    if limit:
        rows = rows[:limit]

    fetched = computed = 0
    for r in rows:
        aid, sport = r[0], r[1]
        if progress:
            progress(str(aid))
        details = _safe(garmin.get_activity_details, aid)
        fetched += 1
        if not details:
            continue
        dec = decoupling_from_details(details, sport)
        if dec is not None:
            db.update_activity_metrics(conn, aid, {"decoupling_pct": round(dec, 2)})
            computed += 1
    return {"eligible": len(rows), "fetched": fetched, "computed": computed}


def run_sync_details(min_minutes: int = 45, limit: int | None = None,
                     mfa_prompt=input, progress=None) -> dict:
    """Log in, compute aerobic decoupling for eligible sessions, store it."""
    db.init_db()
    garmin = connect(mfa_prompt=mfa_prompt)
    with db.connect() as conn:
        summary = sync_activity_details(
            garmin, conn, min_minutes=min_minutes, limit=limit, progress=progress)
        db.log_sync(conn, "sync-details",
                    f"{summary['computed']} decoupling values from "
                    f"{summary['fetched']} fetches",
                    datetime.now().isoformat(timespec="seconds"))
    return summary
