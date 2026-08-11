"""Storage layer — SQLite locally, Postgres when deployed.

Tables that keep your training history durable so trends accumulate over time:

    activities     - one row per workout (swim / bike / run / other)
    daily_metrics  - one row per day of wellness data (RHR, HRV, VO2max, ...)
    sleep          - one row per night of sleep data
    session_tags   - your own read of a session (RPE, talk test)
    garmin_tokens  - cached OAuth tokens, so a serverless run can log in

All upserts are idempotent, so re-syncing an overlapping date range never
creates duplicates — it just refreshes the values.

Set ``DATABASE_URL`` (or ``POSTGRES_URL``) to use Postgres; with neither set it
falls back to a local SQLite file. Serverless filesystems are ephemeral, so a
deployment needs the Postgres path — but tests and local use stay on SQLite,
which needs no server and keeps the suite fast.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from .config import settings


def database_url() -> str | None:
    """Postgres connection string, if one is configured."""
    for var in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING"):
        url = os.getenv(var)
        if url and urlparse(url).scheme in ("postgres", "postgresql"):
            return url
    return None


def is_postgres() -> bool:
    return database_url() is not None


def _ph(sql: str) -> str:
    """Translate ``?`` placeholders to Postgres' ``%s``."""
    return sql.replace("?", "%s") if is_postgres() else sql


def _ddl(sql: str) -> str:
    """Nudge the SQLite schema into Postgres types.

    The schema is deliberately plain, so this stays a handful of swaps rather
    than a second schema to keep in sync.
    """
    if not is_postgres():
        return sql
    return (sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
               .replace("INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY")
               .replace("REAL", "DOUBLE PRECISION")
               .replace("INTEGER", "BIGINT"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id      INTEGER PRIMARY KEY,
    date             TEXT NOT NULL,            -- ISO date (local) YYYY-MM-DD
    start_time       TEXT,                     -- full local timestamp
    sport            TEXT,                     -- swim | bike | run | other
    sub_sport        TEXT,                     -- raw Garmin typeKey
    name             TEXT,
    distance_m       REAL,
    duration_s       REAL,
    moving_s         REAL,
    avg_hr           REAL,
    max_hr           REAL,
    avg_speed_mps    REAL,                     -- meters / second
    elevation_gain_m REAL,
    calories         REAL,
    avg_power_w      REAL,                     -- running power OR cycling power, per sport
    training_load    REAL,                     -- Garmin activityTrainingLoad
    aerobic_te       REAL,
    anaerobic_te     REAL,
    raw              TEXT                       -- full JSON payload
);
CREATE INDEX IF NOT EXISTS idx_activities_date  ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date              TEXT PRIMARY KEY,        -- YYYY-MM-DD
    resting_hr        REAL,
    hrv_overnight     REAL,                    -- avg overnight HRV (ms)
    hrv_status        TEXT,
    stress_avg        REAL,
    body_battery_high REAL,
    body_battery_low  REAL,
    steps             REAL,
    vo2max_run        REAL,
    vo2max_bike       REAL,
    training_status   TEXT,
    weight_kg         REAL,
    raw               TEXT
);

CREATE TABLE IF NOT EXISTS sleep (
    date          TEXT PRIMARY KEY,            -- calendar date of wake-up
    total_sleep_s REAL,
    deep_s        REAL,
    light_s       REAL,
    rem_s         REAL,
    awake_s       REAL,
    sleep_score   REAL,
    raw           TEXT
);

CREATE TABLE IF NOT EXISTS strength_sessions (
    session_id   TEXT PRIMARY KEY,            -- Que activity UUID
    date         TEXT NOT NULL,               -- YYYY-MM-DD (local)
    title        TEXT,
    duration_s   REAL,
    n_exercises  INTEGER,
    n_sets       INTEGER,
    total_reps   REAL,
    tonnage_lb   REAL,                         -- sum(weight*reps) across sets, lbs
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_strength_sessions_date ON strength_sessions(date);

CREATE TABLE IF NOT EXISTS strength_sets (
    session_id  TEXT NOT NULL,               -- FK -> strength_sessions
    date        TEXT NOT NULL,
    exercise    TEXT NOT NULL,
    set_index   INTEGER NOT NULL,
    weight_lb   REAL,
    reps        REAL,
    completed   INTEGER,
    e1rm_lb     REAL,                          -- Epley estimated 1-rep max, lbs
    PRIMARY KEY (session_id, exercise, set_index)
);
CREATE INDEX IF NOT EXISTS idx_strength_sets_date     ON strength_sets(date);
CREATE INDEX IF NOT EXISTS idx_strength_sets_exercise ON strength_sets(exercise);

-- Your own read on a session. The one intensity signal no sensor can corrupt,
-- and the basis of session-RPE load, which is the best-validated training-load
-- measure there is and needs no hardware at all.
--   rpe  : 1-10 Borg CR10, "how hard was that session overall"
--   feel : talk-test bucket -- easy / moderate / hard. "Easy" means you could
--          hold a conversation and breathe through your nose, which is a
--          validated field surrogate for the first ventilatory threshold.
CREATE TABLE IF NOT EXISTS session_tags (
    activity_id INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,
    rpe         REAL,
    feel        TEXT,
    note        TEXT,
    tagged_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_tags_date ON session_tags(date);

-- Cached Garmin OAuth tokens. On a serverless host there is no writable disk
-- to keep .garmintokens in, and no human to type an MFA code, so the token set
-- is seeded once from a local login and refreshed in place from then on.
CREATE TABLE IF NOT EXISTS garmin_tokens (
    name       TEXT PRIMARY KEY,          -- filename inside the token store
    content    TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at    TEXT NOT NULL,
    kind      TEXT,
    detail    TEXT
);
"""

# Columns added after the original schema shipped. Applied with ALTER TABLE on
# every init_db(), so an existing database picks them up without being rebuilt.
# Everything here is already present in the stored ``raw`` JSON of activities
# synced with the old code — run ``python track.py backfill`` to populate them
# without touching Garmin.
MIGRATIONS: dict[str, dict[str, str]] = {
    "activities": {
        "elapsed_s": "REAL",
        # Seconds spent in each heart-rate zone. The basis for training-intensity
        # distribution (polarised / pyramidal), which is the single most
        # evidence-backed thing you can compute from a session.
        "hr_z1_s": "REAL", "hr_z2_s": "REAL", "hr_z3_s": "REAL",
        "hr_z4_s": "REAL", "hr_z5_s": "REAL",
        # Same, by power zone (running power on Garmin watches; cycling power
        # only if a power meter is paired).
        "pwr_z1_s": "REAL", "pwr_z2_s": "REAL", "pwr_z3_s": "REAL",
        "pwr_z4_s": "REAL", "pwr_z5_s": "REAL",
        "norm_power_w": "REAL",
        "intensity_min_mod": "REAL",
        "intensity_min_vig": "REAL",
        "vo2max_activity": "REAL",   # per-activity estimate; denser than the daily one
        # Running dynamics — the raw material for run-economy trends.
        "avg_cadence": "REAL",       # steps/min (run) or strokes/min (swim)
        "avg_stride_m": "REAL",
        "avg_gct_ms": "REAL",        # ground contact time
        "avg_vert_osc_cm": "REAL",
        "avg_vert_ratio": "REAL",
        # Swim.
        "avg_swolf": "REAL",
        "pool_length_m": "REAL",
        # Durability: HR drift vs. output over a long session, in %.
        # Populated by ``track.py sync-details``; NULL until then.
        "decoupling_pct": "REAL",
        # ISO timestamp when this activity was pushed to the Que app
        # (``track.py push-que``). NULL = not sent yet, so a normal push only
        # forwards new activities. The Que server also dedups on externalId.
        "que_pushed_at": "TEXT",
    },
}


def _ensure_columns(conn) -> None:
    """Add any missing columns from MIGRATIONS. Idempotent and non-destructive."""
    for table, cols in MIGRATIONS.items():
        have = _columns(conn, table)
        for col, decl in cols.items():
            if col not in have:
                conn.execute(_ddl(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))


@contextmanager
def connect(db_path: Path | None = None):
    url = database_url()
    if url and db_path is None:
        import psycopg  # imported lazily so local/SQLite use needs no driver

        conn = psycopg.connect(url, autocommit=False)
    else:
        path = db_path or settings.db_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Falling back to SQLite with nowhere to put the file means, on a
            # serverless host, that the Postgres connection string never
            # reached the function. Say that, instead of a bare errno 30 that
            # reads like a bug in the app.
            raise RuntimeError(
                f"No Postgres connection string is configured and the local "
                f"database directory ({path.parent}) cannot be created: {e}. "
                f"A deployment must set DATABASE_URL — serverless filesystems "
                f"are read-only, so the SQLite fallback cannot work there."
            ) from e
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn, table: str) -> set[str]:
    if is_postgres():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s", (table,)).fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    return {r[0] for r in rows}


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                # Executed one at a time: psycopg has no executescript(), and
                # this keeps both backends on the same path.
                conn.execute(_ddl(statement))
        _ensure_columns(conn)


def _upsert(conn, table: str, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != _pk(table))
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({_pk(table)}) DO UPDATE SET {updates}"
    )
    conn.execute(_ph(sql), [_encode(v) for v in row.values()])


def _pk(table: str) -> str:
    return "activity_id" if table == "activities" else "date"


def _encode(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


def upsert_activity(conn, row: dict) -> None:
    _upsert(conn, "activities", row)


def update_activity_metrics(conn, activity_id, fields: dict) -> None:
    """Update only the given columns of one activity, leaving the rest intact.

    Used by the details sync to write back a computed column (e.g.
    ``decoupling_pct``) without re-supplying the whole row.
    """
    if not fields:
        return
    assignments = ", ".join(f"{c}=?" for c in fields)
    conn.execute(
        _ph(f"UPDATE activities SET {assignments} WHERE activity_id=?"),
        [*(_encode(v) for v in fields.values()), activity_id],
    )


def upsert_daily(conn, row: dict) -> None:
    _upsert(conn, "daily_metrics", row)


def upsert_sleep(conn, row: dict) -> None:
    _upsert(conn, "sleep", row)


def upsert_strength_session(conn, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "session_id")
    conn.execute(_ph(
        f"INSERT INTO strength_sessions ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id) DO UPDATE SET {updates}"),
        [_encode(v) for v in row.values()],
    )


def upsert_strength_set(conn, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    key = ("session_id", "exercise", "set_index")
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key)
    conn.execute(_ph(
        f"INSERT INTO strength_sets ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id, exercise, set_index) DO UPDATE SET {updates}"),
        [_encode(v) for v in row.values()],
    )


def log_sync(conn, kind: str, detail: str, ran_at: str) -> None:
    conn.execute(
        _ph("INSERT INTO sync_log (ran_at, kind, detail) VALUES (?, ?, ?)"),
        (ran_at, kind, detail),
    )


# --- Read helpers (return pandas DataFrames for analysis / dashboard) ---

def load_activities(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM activities ORDER BY date", conn,
            parse_dates=["date", "start_time"],
        )
    return df


def load_daily(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM daily_metrics ORDER BY date", conn,
            parse_dates=["date"],
        )
    return df


def load_sleep(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM sleep ORDER BY date", conn, parse_dates=["date"],
        )
    return df


def upsert_session_tag(conn, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "activity_id")
    conn.execute(_ph(
        f"INSERT INTO session_tags ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"),
        [_encode(v) for v in row.values()],
    )


def load_session_tags(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM session_tags ORDER BY date", conn, parse_dates=["date"],
        )


def load_strength_sessions(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM strength_sessions ORDER BY date", conn, parse_dates=["date"],
        )


def load_strength_sets(db_path: Path | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM strength_sets ORDER BY date", conn, parse_dates=["date"],
        )


# --- Garmin token store -----------------------------------------------------

def save_tokens(files: dict[str, str], db_path: Path | None = None) -> int:
    """Persist the contents of a garth token directory."""
    from datetime import datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    init_db(db_path)
    with connect(db_path) as conn:
        for name, content in files.items():
            conn.execute(_ph(
                "INSERT INTO garmin_tokens (name, content, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "content = excluded.content, updated_at = excluded.updated_at"),
                (name, content, now))
    return len(files)


def load_tokens(db_path: Path | None = None) -> dict[str, str]:
    """Token files previously saved, or an empty dict.

    "No tokens yet" and "the database is unreachable" are different answers and
    must not look alike: the first is the normal state before seeding, the
    second is a broken connection string. Only a missing table is swallowed —
    everything else is raised, so a typo in DATABASE_URL reports itself instead
    of quietly presenting as an empty token store.
    """
    try:
        with connect(db_path) as conn:
            rows = conn.execute("SELECT name, content FROM garmin_tokens").fetchall()
    except Exception as e:
        if _is_missing_table(e):
            return {}
        raise
    return {r[0]: r[1] for r in rows}


def _is_missing_table(exc: Exception) -> bool:
    """True when the failure is just 'garmin_tokens doesn't exist yet'.

    Matched on the message rather than the exception class so it holds for both
    backends without importing psycopg at module scope: sqlite3 raises
    OperationalError("no such table: ..."), psycopg UndefinedTable("relation
    ... does not exist").
    """
    msg = str(exc).lower()
    return "no such table" in msg or "does not exist" in msg
