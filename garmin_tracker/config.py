"""Settings loaded from the environment / .env file."""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of this package directory.
ROOT = Path(__file__).resolve().parent.parent

# Load .env from the project root if present.
load_dotenv(ROOT / ".env")


def _resolve(path_str: str) -> Path:
    """Resolve a path relative to the project root unless it is absolute."""
    p = Path(os.path.expanduser(path_str))
    return p if p.is_absolute() else ROOT / p


def _try_mkdir(path: Path) -> Path:
    """Create a directory, tolerating a read-only filesystem.

    Serverless hosts mount the deployment read-only — only ``/tmp`` is
    writable. Neither of these directories is used there (storage is Postgres
    and the Garmin tokens live in a table), but this runs at import time, so an
    uncaught ``OSError`` would take the whole app down before it served a
    single request rather than failing later and locally.
    """
    with suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Settings:
    email: str | None
    password: str | None
    tokenstore: Path
    db_path: Path
    race_date: str | None       # ISO YYYY-MM-DD of goal race, or None
    race_name: str | None
    weekly_session_target: int  # sessions/week the Consistency pillar aims for
    que_export_path: Path       # exported Que localStorage JSON (ironmanCoreDB_v2)
    que_activity_url: str | None    # Que /api/health/activity endpoint (cardio push)
    que_activity_token: str | None  # personal health-sync token from the Que app

    @classmethod
    def load(cls) -> Settings:
        db_path = _resolve(os.getenv("GARMIN_DB", "data/garmin.db"))
        tokenstore = _resolve(os.getenv("GARMINTOKENS", ".garmintokens"))
        # Make sure the parent folders exist. Best-effort: see _try_mkdir.
        _try_mkdir(db_path.parent)
        _try_mkdir(tokenstore)
        return cls(
            email=os.getenv("GARMIN_EMAIL"),
            password=os.getenv("GARMIN_PASSWORD"),
            tokenstore=tokenstore,
            db_path=db_path,
            race_date=os.getenv("GARMIN_RACE_DATE") or None,
            race_name=os.getenv("GARMIN_RACE_NAME") or "Race day",
            weekly_session_target=int(os.getenv("GARMIN_WEEKLY_SESSIONS", "6")),
            que_export_path=_resolve(os.getenv("QUE_EXPORT_PATH", "data/que_export.json")),
            que_activity_url=os.getenv("QUE_ACTIVITY_URL") or None,
            que_activity_token=os.getenv("QUE_ACTIVITY_TOKEN") or None,
        )


settings = Settings.load()
