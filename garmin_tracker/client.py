"""Authenticated Garmin Connect session.

Uses a cached OAuth token store so you only enter credentials (and MFA) once.
This is the same auth model the Taxuspt/garmin_mcp server uses.
"""

from __future__ import annotations

from pathlib import Path

import logging
import tempfile

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .config import settings

log = logging.getLogger(__name__)

# Failures that mean "these tokens are no good" and justify a fresh login.
# Anything else — a dropped connection, a 429, a Garmin outage — must NOT
# trigger a credential login: repeatedly re-authenticating during an outage is
# what gets a Garmin account temporarily locked out.
_TOKEN_ERRORS = (FileNotFoundError, KeyError, ValueError,
                 GarminConnectAuthenticationError)


def _hydrate_tokenstore() -> Path:
    """Materialise the token store on disk, pulling from the database if needed.

    On a serverless host the repo is read-only and nothing survives between
    invocations, so tokens live in the database and are written into a temp
    directory for the duration of the call. Locally the normal
    ``.garmintokens`` folder is used unchanged.
    """
    local = Path(settings.tokenstore)
    if any(local.glob("*")):
        return local

    from . import db

    stored = db.load_tokens()
    if not stored:
        return local
    target = Path(tempfile.mkdtemp(prefix="garmintokens-"))
    for name, content in stored.items():
        (target / name).write_text(content, encoding="utf-8")
    log.info("Hydrated %d Garmin token file(s) from the database.", len(stored))
    return target


def _persist_tokenstore(tokenstore: Path) -> None:
    """Save the token store back, so refreshed tokens survive the invocation."""
    from . import db

    files = {p.name: p.read_text(encoding="utf-8")
             for p in Path(tokenstore).glob("*") if p.is_file()}
    if not files:
        return
    try:
        db.save_tokens(files)
    except Exception as e:  # never let token bookkeeping fail a sync
        log.warning("Could not persist Garmin tokens (%s: %s).",
                    type(e).__name__, e)


def connect(mfa_prompt=input, persist: bool | None = None) -> Garmin:
    """Return a logged-in Garmin client.

    Tries the cached token store first. If the tokens are missing or rejected,
    logs in with email/password and handles MFA, then caches tokens. Transient
    network problems are re-raised rather than being retried as a fresh login.

    Args:
        mfa_prompt: callable returning the MFA code as a string. Defaults to
            the builtin ``input``. Pass a custom callable for non-interactive
            contexts.
        persist: write the token store back to the database afterwards.
            Defaults to on whenever a database-backed store is in play.
    """
    store = _hydrate_tokenstore()
    tokenstore = str(store)
    if persist is None:
        from . import db
        persist = db.is_postgres() or store != Path(settings.tokenstore)

    # 1) Try resuming from cached tokens.
    try:
        garmin = Garmin()
        garmin.login(tokenstore)
        if persist:
            _persist_tokenstore(store)  # login refreshes them in place
        return garmin
    except (GarminConnectConnectionError, GarminConnectTooManyRequestsError):
        raise  # transient — surface it instead of burning a login attempt
    except _TOKEN_ERRORS as e:
        log.info("Cached Garmin tokens unusable (%s); logging in fresh.", e)
    except Exception as e:  # unknown shape from the unofficial API
        log.warning("Token login failed (%s: %s); logging in fresh.",
                    type(e).__name__, e)

    # 2) Fresh login with credentials.
    if not settings.email or not settings.password:
        raise RuntimeError(
            "No cached Garmin tokens and GARMIN_EMAIL / GARMIN_PASSWORD are not "
            "set. Copy .env.example to .env and fill in your credentials."
        )

    garmin = Garmin(
        email=settings.email,
        password=settings.password,
        return_on_mfa=True,
    )
    result1, result2 = garmin.login()

    if result1 == "needs_mfa":
        code = mfa_prompt("Enter the Garmin MFA code sent to you: ").strip()
        garmin.resume_login(result2, code)

    # Cache tokens for next time.
    Path(tokenstore).mkdir(parents=True, exist_ok=True)
    garmin.client.dump(tokenstore)
    if persist:
        _persist_tokenstore(Path(tokenstore))
    return garmin
