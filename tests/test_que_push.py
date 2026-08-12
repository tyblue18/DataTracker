"""Unit tests for the Garmin -> Que cardio push mapping (pure function)."""

from __future__ import annotations

from garmin_tracker.que_push import activity_to_payload


def test_run_maps_metres_and_seconds_to_km_and_minutes():
    p = activity_to_payload({
        "activity_id": 42, "sport": "run", "date": "2026-08-01",
        "distance_m": 5000, "duration_s": 1500, "moving_s": 1500,
    })
    assert p == {
        "type": "run", "time": 25.0, "date": "2026-08-01",
        "externalId": "garmin-42", "distance": 5.0, "unit": "km",
    }


def test_prefers_moving_time_over_elapsed():
    p = activity_to_payload({
        "activity_id": 1, "sport": "bike", "date": "2026-08-01",
        "distance_m": 20000, "duration_s": 3600, "moving_s": 3000,
    })
    assert p["time"] == 50.0  # 3000s, not 3600s


def test_falls_back_to_duration_when_moving_missing():
    p = activity_to_payload({
        "activity_id": 2, "sport": "bike", "date": "2026-08-01",
        "distance_m": 20000, "duration_s": 3600,  # no moving_s key
    })
    assert p["time"] == 60.0


def test_swim_is_time_only_allowed_without_distance():
    p = activity_to_payload({
        "activity_id": 3, "sport": "swim", "date": "2026-08-01",
        "distance_m": 0, "duration_s": 1800, "moving_s": None,
    })
    assert p["type"] == "swim" and p["time"] == 30.0 and "distance" not in p


def test_swim_keeps_distance_when_present():
    p = activity_to_payload({
        "activity_id": 4, "sport": "swim", "date": "2026-08-01",
        "distance_m": 1500, "moving_s": 1800,
    })
    assert p["distance"] == 1.5 and p["unit"] == "km"


def test_other_sport_is_skipped():
    assert activity_to_payload({
        "activity_id": 5, "sport": "other", "date": "2026-08-01", "duration_s": 600,
    }) is None


def test_distance_less_indoor_ride_is_sent_time_only():
    # Indoor rides often carry no distance — send time (+ calories when present)
    # instead of dropping the workout.
    p = activity_to_payload({
        "activity_id": 6, "sport": "bike", "date": "2026-08-01",
        "distance_m": 0, "duration_s": 1920,
        "calories": 228, "bmr_calories": 44,
    })
    assert p["type"] == "bike" and p["time"] == 32.0
    assert "distance" not in p
    assert p["calories"] == 184


def test_zero_duration_is_skipped():
    assert activity_to_payload({
        "activity_id": 7, "sport": "run", "date": "2026-08-01",
        "distance_m": 5000, "duration_s": 0, "moving_s": 0,
    }) is None


def test_date_is_truncated_to_ten_chars():
    p = activity_to_payload({
        "activity_id": 8, "sport": "run", "date": "2026-08-01T07:30:00",
        "distance_m": 5000, "moving_s": 1500,
    })
    assert p["date"] == "2026-08-01"


def test_active_calories_are_total_minus_bmr():
    # Garmin stores TOTAL calories; active = total - bmrCalories.
    p = activity_to_payload({
        "activity_id": 9, "sport": "bike", "date": "2026-08-01",
        "distance_m": 20000, "moving_s": 3600,
        "calories": 800, "bmr_calories": 120,
    })
    assert p["calories"] == 680


def test_no_calories_key_when_active_is_zero_or_missing():
    p = activity_to_payload({
        "activity_id": 10, "sport": "run", "date": "2026-08-01",
        "distance_m": 5000, "moving_s": 1500,  # no calorie fields
    })
    assert "calories" not in p
