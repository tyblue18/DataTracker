"""Tests for the Garmin payload parsers.

``sync.py`` is the layer most exposed to Garmin's "payload shapes drift between
account types and firmware versions" problem — and, being network-facing, the
one the rest of the suite doesn't reach. These tests pin the field mapping, the
unit conversions (which are silent when wrong), and the defensive handling of
missing/None fields, all against hand-built payloads so nothing here touches
Garmin.
"""

from __future__ import annotations

from datetime import date

import pytest

from garmin_tracker import db
from garmin_tracker.sync import (
    _num,
    _pool_length_m,
    _settled_days,
    _sport,
    decoupling_from_details,
    extract_streams,
    parse_activity,
    parse_daily,
    parse_sleep,
)

# ---------------------------------------------------------------------------
# _num
# ---------------------------------------------------------------------------

def test_num_coerces_and_defends():
    assert _num(5) == 5.0
    assert _num("12.5") == 12.5
    assert _num(None) is None
    assert _num("not a number") is None
    assert _num([]) is None


# ---------------------------------------------------------------------------
# _sport
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("type_key,expected", [
    ("running", "run"),
    ("treadmill_running", "run"),
    ("trail_running", "run"),
    ("cycling", "bike"),
    ("virtual_ride", "bike"),
    ("gravel_cycling", "bike"),
    ("lap_swimming", "swim"),
    ("open_water_swimming", "swim"),
    ("strength_training", "other"),
    ("unknown_future_sport", "other"),
    (None, "other"),
])
def test_sport_mapping(type_key, expected):
    assert _sport(type_key) == expected


# ---------------------------------------------------------------------------
# _pool_length_m — the unit conversion that is wrong silently
# ---------------------------------------------------------------------------

def test_pool_length_metres_passthrough():
    a = {"poolLength": 25.0, "unitOfPoolLength": {"unitKey": "meter", "factor": 1.0}}
    assert _pool_length_m(a) == 25.0


def test_pool_length_yards_converted_to_metres():
    a = {"poolLength": 25.0, "unitOfPoolLength": {"unitKey": "yard", "factor": 1.0}}
    # 25 yd -> 22.86 m
    assert _pool_length_m(a) == pytest.approx(22.86, abs=0.01)


def test_pool_length_absent_is_none():
    assert _pool_length_m({}) is None


# ---------------------------------------------------------------------------
# parse_activity
# ---------------------------------------------------------------------------

def _run_payload() -> dict:
    return {
        "activityId": 123,
        "activityType": {"typeKey": "running"},
        "activityName": "Morning Run",
        "startTimeLocal": "2026-07-21 06:30:00",
        "distance": 10000.0,
        "duration": 3000.0,
        "averageHR": 150.0,
        "maxHR": 175.0,
        "averageSpeed": 3.33,
        "activityTrainingLoad": 120.0,
        "averageRunningCadenceInStepsPerMinute": 172.0,
        "avgStrideLength": 118.0,  # centimetres
        "avgPower": 280.0,
        "hrTimeInZone_1": 100.0, "hrTimeInZone_2": 200.0, "hrTimeInZone_3": 300.0,
        "hrTimeInZone_4": 400.0, "hrTimeInZone_5": 500.0,
        "powerTimeInZone_1": 10.0, "powerTimeInZone_2": 20.0, "powerTimeInZone_3": 30.0,
        "powerTimeInZone_4": 40.0, "powerTimeInZone_5": 50.0,
    }


def test_parse_activity_maps_core_fields():
    row = parse_activity(_run_payload())
    assert row["activity_id"] == 123
    assert row["date"] == "2026-07-21"          # split off the time
    assert row["sport"] == "run"
    assert row["sub_sport"] == "running"
    assert row["distance_m"] == 10000.0
    assert row["training_load"] == 120.0
    assert row["avg_cadence"] == 172.0


def test_parse_activity_converts_stride_cm_to_m():
    assert parse_activity(_run_payload())["avg_stride_m"] == pytest.approx(1.18)


def test_parse_activity_reads_time_in_zone():
    row = parse_activity(_run_payload())
    assert [row[f"hr_z{i}_s"] for i in range(1, 6)] == [100, 200, 300, 400, 500]
    assert [row[f"pwr_z{i}_s"] for i in range(1, 6)] == [10, 20, 30, 40, 50]


def test_parse_activity_reads_bike_cadence_key():
    a = {"activityType": {"typeKey": "cycling"},
         "averageBikingCadenceInRevsPerMinute": 90.0}
    row = parse_activity(a)
    assert row["sport"] == "bike"
    assert row["avg_cadence"] == 90.0


def test_parse_activity_power_falls_back_to_averagePower_spelling():
    # Some payloads use averagePower rather than avgPower; both must map.
    assert parse_activity({"averagePower": 240.0})["avg_power_w"] == 240.0


def test_parse_activity_uses_gmt_when_local_missing():
    row = parse_activity({"startTimeGMT": "2026-07-21 05:30:00"})
    assert row["date"] == "2026-07-21"


def test_parse_activity_on_empty_payload_does_not_raise():
    row = parse_activity({})
    assert row["activity_id"] is None
    assert row["sport"] == "other"
    assert row["date"] == ""
    assert row["avg_stride_m"] is None          # (None or 0)/100 or None
    assert row["pool_length_m"] is None


# ---------------------------------------------------------------------------
# parse_daily
# ---------------------------------------------------------------------------

def test_parse_daily_maps_all_sources():
    stats = {"restingHeartRate": 48, "averageStressLevel": 30,
             "bodyBatteryHighestValue": 95, "bodyBatteryLowestValue": 20,
             "totalSteps": 12000, "weight": 70000.0}  # weight in grams
    hrv = {"hrvSummary": {"lastNightAvg": 65.0, "status": "BALANCED"}}
    vo2 = [{"generic": {"vo2MaxPreciseValue": 54.3},
            "cycling": {"vo2MaxPreciseValue": 58.1}}]
    training = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {
        "3917xxxx": {"trainingStatusFeedbackPhrase": "PRODUCTIVE_1"}}}}

    row = parse_daily("2026-07-21", stats, hrv, vo2, training)
    assert row["date"] == "2026-07-21"
    assert row["resting_hr"] == 48.0
    assert row["hrv_overnight"] == 65.0
    assert row["hrv_status"] == "BALANCED"
    assert row["vo2max_run"] == 54.3
    assert row["vo2max_bike"] == 58.1
    assert row["training_status"] == "PRODUCTIVE_1"
    assert row["weight_kg"] == pytest.approx(70.0)   # grams -> kg


def test_parse_daily_vo2_precise_value_falls_back_to_plain():
    vo2 = [{"generic": {"vo2MaxValue": 52.0}}]
    row = parse_daily("2026-07-21", {}, None, vo2, None)
    assert row["vo2max_run"] == 52.0
    assert row["vo2max_bike"] is None


def test_parse_daily_weight_absent_is_none():
    assert parse_daily("2026-07-21", {"restingHeartRate": 50}, None, None,
                       None)["weight_kg"] is None


def test_parse_daily_all_none_does_not_raise():
    row = parse_daily("2026-07-21", None, None, None, None)
    assert row["date"] == "2026-07-21"
    assert row["resting_hr"] is None
    assert row["hrv_overnight"] is None
    assert row["vo2max_run"] is None
    assert row["training_status"] is None


# ---------------------------------------------------------------------------
# parse_sleep
# ---------------------------------------------------------------------------

def test_parse_sleep_maps_stages_and_score():
    sleep = {"dailySleepDTO": {
        "sleepTimeSeconds": 27000, "deepSleepSeconds": 6000,
        "lightSleepSeconds": 15000, "remSleepSeconds": 6000,
        "awakeSleepSeconds": 600,
        "sleepScores": {"overall": {"value": 82}}}}
    row = parse_sleep("2026-07-21", sleep)
    assert row["total_sleep_s"] == 27000.0
    assert row["deep_s"] == 6000.0
    assert row["sleep_score"] == 82.0


def test_parse_sleep_none_when_not_a_dict():
    assert parse_sleep("2026-07-21", None) is None


def test_parse_sleep_none_when_dto_missing():
    assert parse_sleep("2026-07-21", {"dailySleepDTO": {}}) is None


# ---------------------------------------------------------------------------
# extract_streams / decoupling_from_details (per-activity detail payloads)
# ---------------------------------------------------------------------------

def _details(hr, power=None, speed=None, times=None) -> dict:
    """Build a Garmin activity-details payload (column store) from series."""
    times = times if times is not None else list(range(len(hr)))
    cols = [("sumElapsedDuration", times), ("directHeartRate", hr)]
    if power is not None:
        cols.append(("directPower", power))
    if speed is not None:
        cols.append(("directSpeed", speed))
    descriptors = [{"key": k, "metricsIndex": i} for i, (k, _) in enumerate(cols)]
    n = len(hr)
    samples = [{"metrics": [col[r] for _, col in cols]} for r in range(n)]
    return {"metricDescriptors": descriptors, "activityDetailMetrics": samples}


def test_extract_streams_pulls_named_columns_in_order():
    payload = _details(hr=[140, 141, 142], power=[200, 201, 202])
    s = extract_streams(payload)
    assert s["hr"] == [140, 141, 142]
    assert s["power"] == [200, 201, 202]
    assert s["speed"] == []          # absent stream comes back empty
    assert s["time"] == [0, 1, 2]


def test_extract_streams_on_empty_payload_is_all_empty():
    s = extract_streams({})
    assert s["hr"] == [] and s["power"] == [] and s["time"] == []


def test_decoupling_from_details_uses_power_and_sees_drift():
    payload = _details(power=[200.0] * 100, hr=[140.0] * 50 + [154.0] * 50)
    d = decoupling_from_details(payload, "bike")
    assert d is not None and d > 5


def test_decoupling_from_details_falls_back_to_speed():
    payload = _details(speed=[3.3] * 100, hr=[140.0] * 50 + [154.0] * 50)
    assert decoupling_from_details(payload, "run") > 5


def test_decoupling_from_details_skips_swims():
    payload = _details(speed=[1.2] * 100, hr=[130.0] * 100)
    assert decoupling_from_details(payload, "swim") is None


def test_decoupling_from_details_none_without_usable_output():
    payload = _details(hr=[140.0] * 100)  # HR only, no power or speed
    assert decoupling_from_details(payload, "run") is None


# ---------------------------------------------------------------------------
# Cross-backend query safety (?-placeholders must become %s on Postgres)
# ---------------------------------------------------------------------------

def test_ph_translates_question_marks_only_on_postgres(monkeypatch):
    monkeypatch.setattr(db, "is_postgres", lambda: True)
    assert db._ph("WHERE a = ? AND b = ?") == "WHERE a = %s AND b = %s"
    monkeypatch.setattr(db, "is_postgres", lambda: False)
    assert db._ph("WHERE a = ?") == "WHERE a = ?"


def test_settled_days_returns_only_fully_filled_past_days(tmp_path):
    """A regression guard for the _settled_days query (SQLite path).

    Also documents the contract: a day counts as settled only when both its
    wellness and its sleep rows are present and it is older than the recheck
    window.
    """
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    with db.connect(dbfile) as conn:
        # Fully filled, well in the past -> settled.
        db.upsert_daily(conn, {"date": "2026-05-01", "resting_hr": 50})
        db.upsert_sleep(conn, {"date": "2026-05-01", "total_sleep_s": 27000})
        # Wellness but no sleep yet -> not settled.
        db.upsert_daily(conn, {"date": "2026-05-02", "resting_hr": 51})

    with db.connect(dbfile) as conn:
        settled = _settled_days(conn, date(2026, 5, 1), date(2026, 5, 10),
                                recheck_days=3)
    assert settled == {"2026-05-01"}


# ---------------------------------------------------------------------------
# Token store error reporting
# ---------------------------------------------------------------------------

def test_missing_table_reads_as_an_empty_token_store(tmp_path):
    """Before seeding, no tokens is the normal answer, not a failure."""
    fresh = tmp_path / "empty.db"
    import sqlite3
    sqlite3.connect(fresh).close()          # a database with no schema at all
    assert db.load_tokens(fresh) == {}


def test_an_unreachable_database_raises_instead_of_reporting_zero(monkeypatch):
    """A bad connection string must not masquerade as 'you haven't seeded yet'."""
    import pytest

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "connect", boom)
    with pytest.raises(RuntimeError, match="connection refused"):
        db.load_tokens()


def test_missing_table_is_recognised_for_both_backends():
    assert db._is_missing_table(Exception("no such table: garmin_tokens"))
    assert db._is_missing_table(
        Exception('relation "garmin_tokens" does not exist'))
    assert not db._is_missing_table(Exception("connection refused"))
    assert not db._is_missing_table(Exception("password authentication failed"))


def test_tokens_round_trip_through_the_store(tmp_path):
    p = tmp_path / "t.db"
    db.save_tokens({"garmin_tokens.json": '{"a": 1}'}, p)
    assert db.load_tokens(p) == {"garmin_tokens.json": '{"a": 1}'}
