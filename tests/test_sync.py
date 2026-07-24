"""Tests for the Garmin payload parsers.

``sync.py`` is the layer most exposed to Garmin's "payload shapes drift between
account types and firmware versions" problem — and, being network-facing, the
one the rest of the suite doesn't reach. These tests pin the field mapping, the
unit conversions (which are silent when wrong), and the defensive handling of
missing/None fields, all against hand-built payloads so nothing here touches
Garmin.
"""

from __future__ import annotations

import pytest

from garmin_tracker.sync import (
    _num,
    _pool_length_m,
    _sport,
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
