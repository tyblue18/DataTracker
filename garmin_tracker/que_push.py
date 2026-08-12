"""Push Garmin cardio activities to the Que app.

The mirror image of ``que_sync`` (which imports strength *from* a Que export):
this reads run/bike/swim rows from the local ``activities`` table and POSTs each
to Que's personal-token activity endpoint (``/api/health/activity``), so cardio
distance/time — and therefore calories, which Que computes itself — appear in
Que automatically with no manual entry.

Idempotent on both ends:
  * each POST carries ``externalId = "garmin-<activity_id>"``, which the Que
    server dedups (a re-sent activity is a no-op, never a double-count);
  * locally we stamp ``activities.que_pushed_at`` after a successful send, so a
    normal run only forwards activities that haven't been pushed yet.

Config (``.env``): ``QUE_ACTIVITY_URL`` + ``QUE_ACTIVITY_TOKEN`` (the token comes
from Que -> Metrics tab -> "Auto-sync from your watch" -> Copy token).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import requests

from . import db
from .config import settings

# Garmin's normalised sport -> Que activity type. Anything else ('other':
# strength, yoga, walk, ...) is skipped — the endpoint only models these three.
_SPORT_TO_TYPE = {"run": "run", "bike": "bike", "swim": "swim"}


class QueNotConfigured(RuntimeError):
    """QUE_ACTIVITY_URL / QUE_ACTIVITY_TOKEN aren't set."""


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def activity_to_payload(row: dict) -> dict | None:
    """Map one ``activities`` row to a Que ``/api/health/activity`` body.

    Returns ``None`` to skip the row (non-cardio sport, zero duration, or a
    run/bike with no distance — all of which the server would reject). PURE.

    Units: Garmin stores metres + seconds; we send distance in km (the server
    converts to miles) and time in minutes.
    """
    qtype = _SPORT_TO_TYPE.get(str(row.get("sport") or "").lower())
    if qtype is None:
        return None

    # Prefer moving time (excludes stops) for a truer effort/calorie estimate.
    minutes = round((_num(row.get("moving_s")) or _num(row.get("duration_s"))) / 60.0, 1)
    if minutes <= 0:
        return None

    # Distance is optional for every type — indoor rides / treadmill sessions
    # often have none. Time + measured calories is all Que needs for those.
    dist_m = _num(row.get("distance_m"))

    payload: dict = {
        "type": qtype,
        "time": minutes,
        "date": str(row.get("date"))[:10],            # YYYY-MM-DD (local start)
        "externalId": f"garmin-{row['activity_id']}",  # idempotent on the server
    }
    if dist_m > 0:
        payload["distance"] = round(dist_m / 1000.0, 3)
        payload["unit"] = "km"

    # Garmin's `calories` is TOTAL (includes resting); `bmrCalories` is the
    # resting part, so ACTIVE = total - bmr — the "active calories" the watch
    # shows, and what Que treats as the net activity burn (HR/power based, far
    # better than a distance estimate, especially on the bike).
    active = _num(row.get("calories")) - _num(row.get("bmr_calories"))
    if active > 0:
        payload["calories"] = round(active)
    return payload


def _bmr_from_raw(raw) -> float:
    """Garmin's resting-calorie portion (`bmrCalories`) out of the stored raw JSON."""
    try:
        return float(json.loads(raw).get("bmrCalories") or 0)
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return 0.0


def _rows_since(conn, since: str, resend: bool) -> list[dict]:
    """Fetch activity rows as plain dicts (works on SQLite and Postgres)."""
    cols = db._columns(conn, "activities")
    where = "date >= ?"
    if not resend and "que_pushed_at" in cols:
        where += " AND que_pushed_at IS NULL"
    cur = conn.execute(
        db._ph(f"SELECT * FROM activities WHERE {where} ORDER BY date"), (since,)
    )
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    for row in rows:
        row["bmr_calories"] = _bmr_from_raw(row.get("raw"))  # derived from raw JSON
    return rows


def push_activities(days: int = 30, resend: bool = False) -> dict:
    """POST recent Garmin cardio to Que. Returns a summary dict.

    Raises :class:`QueNotConfigured` if the endpoint/token aren't set.
    """
    url, token = settings.que_activity_url, settings.que_activity_token
    if not url or not token:
        raise QueNotConfigured(
            "set QUE_ACTIVITY_URL and QUE_ACTIVITY_TOKEN in .env "
            "(token: Que -> Metrics -> Auto-sync from your watch -> Copy token)"
        )

    db.init_db()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.connect() as conn:
        rows = _rows_since(conn, since, resend)

    sent = skipped = failed = 0
    pushed_ids: list = []
    with requests.Session() as sess:
        sess.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        for row in rows:
            payload = activity_to_payload(row)
            if payload is None:
                skipped += 1
                continue
            try:
                resp = sess.post(url, json=payload, timeout=20)
            except requests.RequestException as e:
                failed += 1
                print(f"  x {payload['externalId']}: {e}")
                continue
            if resp.ok:
                sent += 1
                pushed_ids.append(row["activity_id"])
            else:
                failed += 1
                body = resp.text[:140].replace("\n", " ")
                print(f"  x {payload['externalId']}: {resp.status_code} {body}")

    now = datetime.now(UTC).isoformat()
    with db.connect() as conn:
        if pushed_ids and "que_pushed_at" in db._columns(conn, "activities"):
            for aid in pushed_ids:
                db.update_activity_metrics(conn, aid, {"que_pushed_at": now})
        db.log_sync(conn, "que-push",
                    f"{sent} sent, {skipped} skipped, {failed} failed", now)

    return {"sent": sent, "skipped": skipped, "failed": failed, "considered": len(rows)}
