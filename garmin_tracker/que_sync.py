"""Import strength-training data from the Que workout app.

Que (the web redesign) has **no backend** — it stores everything in the
browser's ``localStorage`` under the key ``ironmanCoreDB_v2``. That value is a
JSON object keyed by date, and each day's ``exercises`` field is itself a
JSON-serialised array of entries:

    { "2026-06-30": {
        "exercises": "[{\\"k\\":\\"lift\\",\\"g\\":\\"Chest\\",\\"n\\":\\"Bench Press\\",
                        \\"sets\\":[{\\"r\\":\\"10\\",\\"w\\":\\"135\\"}]}]",
        "weight": "180", ... },
      ... }

To get it out of the browser: open the Que web app, DevTools console, run
``copy(localStorage.getItem('ironmanCoreDB_v2'))`` and paste into the export
file (default ``data/que_export.json``). This importer reads that file — it is
tolerant of the raw string, the parsed object, or a ``{"ironmanCoreDB_v2": ...}``
wrapper. Weights are pounds (free-text like "135 lbs" is fine); we never write
back to Que.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import db
from .config import settings

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class QueUnavailable(RuntimeError):
    """Raised when the Que export file is missing or unreadable."""


def epley_1rm(weight: float, reps: float) -> float:
    """Estimated one-rep max (Epley). reps<=1 returns the weight itself."""
    if weight <= 0 or reps <= 0:
        return 0.0
    return weight * (1.0 + reps / 30.0)


def _num(x) -> float | None:
    """Pull the first number out of free-text like '135 lbs' or '10'."""
    if x is None:
        return None
    m = _NUM_RE.search(str(x))
    return float(m.group()) if m else None


def _iter_sets(ex: dict):
    """Yield (weight, reps) pairs for a lift entry, new or legacy format."""
    sets = ex.get("sets")
    if isinstance(sets, list) and sets:
        for s in sets:
            yield _num(s.get("w")), _num(s.get("r"))
    else:  # legacy single-line: s = set count, r = reps, w = weight
        w, r = _num(ex.get("w")), _num(ex.get("r"))
        count = int(_num(ex.get("s")) or 1)
        for _ in range(max(1, count)):
            yield w, r


def _coerce_db(data) -> dict:
    """Normalise whatever is in the export file to the date->DayRecord dict."""
    # Unwrap a raw string (the localStorage value pasted directly).
    if isinstance(data, str):
        data = json.loads(data)
    # Unwrap {"ironmanCoreDB_v2": <db or string>}.
    if isinstance(data, dict) and "ironmanCoreDB_v2" in data:
        inner = data["ironmanCoreDB_v2"]
        data = json.loads(inner) if isinstance(inner, str) else inner
    if not isinstance(data, dict):
        raise QueUnavailable("Export isn't a date-keyed object — check the file.")
    return data


def parse_local_db(data) -> tuple[list[dict], list[dict]]:
    """Turn the exported local DB into (session_rows, set_rows) for SQLite.

    One gym "session" per date that has any lift entries. Pure function.
    """
    localdb = _coerce_db(data)
    sessions, sets = [], []

    for date, record in localdb.items():
        if not (_DATE_RE.match(str(date)) and isinstance(record, dict)):
            continue
        raw_ex = record.get("exercises")
        if not raw_ex:
            continue
        try:
            entries = json.loads(raw_ex) if isinstance(raw_ex, str) else raw_ex
        except (json.JSONDecodeError, TypeError):
            continue
        lifts = [e for e in entries if isinstance(e, dict) and e.get("k") == "lift"]
        if not lifts:
            continue

        n_sets = total_reps = tonnage = 0
        for ex in lifts:
            name = (ex.get("n") or "Unknown").strip()
            group = (ex.get("g") or "").strip()
            for i, (w, r) in enumerate(_iter_sets(ex)):
                if w is None or r is None:
                    continue
                n_sets += 1
                total_reps += r
                tonnage += w * r
                sets.append({
                    "session_id": date, "date": date, "exercise": name,
                    "set_index": i, "weight_lb": w, "reps": r,
                    "completed": 1, "e1rm_lb": round(epley_1rm(w, r), 1),
                    "muscle_group": group,
                })
        if n_sets == 0:
            continue
        sessions.append({
            "session_id": date, "date": date, "title": "Gym Session",
            "duration_s": None, "n_exercises": len(lifts), "n_sets": n_sets,
            "total_reps": total_reps, "tonnage_lb": round(tonnage, 1),
            "raw": {"exercises": lifts},
        })
    return sessions, sets


def _read_export() -> object:
    path = settings.que_export_path
    if not path.exists():
        raise QueUnavailable(
            f"No Que export found at {path}. In the Que web app open DevTools "
            "console, run  copy(localStorage.getItem('ironmanCoreDB_v2'))  and "
            f"paste the result into that file."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise QueUnavailable(f"Couldn't read {path}: {e}") from e


def sync_strength(data: object | None = None) -> dict:
    """Import lifts from the Que export into the local DB. Returns a summary.

    Pass ``data`` (parsed export) directly for tests; otherwise it's read from
    ``settings.que_export_path``. Strength-set rows carry a ``muscle_group`` the
    sets table ignores unless present, so only known columns are written.
    """
    data = _read_export() if data is None else data
    session_rows, set_rows = parse_local_db(data)

    db.init_db()
    with db.connect() as conn:
        set_cols = {r[1] for r in conn.execute("PRAGMA table_info(strength_sets)")}
        for s in session_rows:
            db.upsert_strength_session(conn, s)
        for s in set_rows:
            db.upsert_strength_set(conn, {k: v for k, v in s.items() if k in set_cols})
        db.log_sync(conn, "que", f"{len(session_rows)} sessions, {len(set_rows)} sets",
                    datetime.now(timezone.utc).isoformat())

    dates = [s["date"] for s in session_rows if s["date"]]
    return {
        "sessions": len(session_rows),
        "sets": len(set_rows),
        "start": min(dates) if dates else None,
        "end": max(dates) if dates else None,
    }
