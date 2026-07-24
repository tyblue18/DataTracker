"""Settings loaded from the environment / .env file."""

from __future__ import annotations

import os
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

    @classmethod
    def load(cls) -> "Settings":
        db_path = _resolve(os.getenv("GARMIN_DB", "data/garmin.db"))
        tokenstore = _resolve(os.getenv("GARMINTOKENS", ".garmintokens"))
        # Make sure the parent folders exist.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tokenstore.mkdir(parents=True, exist_ok=True)
        return cls(
            email=os.getenv("GARMIN_EMAIL"),
            password=os.getenv("GARMIN_PASSWORD"),
            tokenstore=tokenstore,
            db_path=db_path,
            race_date=os.getenv("GARMIN_RACE_DATE") or None,
            race_name=os.getenv("GARMIN_RACE_NAME") or "Race day",
            weekly_session_target=int(os.getenv("GARMIN_WEEKLY_SESSIONS", "6")),
            que_export_path=_resolve(os.getenv("QUE_EXPORT_PATH", "data/que_export.json")),
        )


settings = Settings.load()
