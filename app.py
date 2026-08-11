"""Vercel entrypoint — the dashboard as a web service.

Routes
------
GET  /                     the rendered dashboard   (public by default)
POST /api/sync             pull latest from Garmin  (owner only)
POST /api/sync/details     backfill decoupling      (owner only)
GET  /api/snapshot         the same numbers as JSON (owner only)
GET  /api/cron/sync        scheduled sync           (CRON_SECRET)
GET  /api/cron/sync-details scheduled decoupling    (CRON_SECRET)
GET  /api/health           liveness + config check  (public, no data)

Auth
----
The dashboard is meant to be shared — a link you can send a friend, no login.
So reads are open and *writes* are not: the two things worth protecting are
the sync button, which spends a finite Garmin API rate limit, and the raw JSON
snapshot. Both sit behind ``APP_PASSWORD``, which you enter once on your own
device and which is then remembered in a cookie.

Set ``PUBLIC_DASHBOARD=0`` to put the whole page behind that password instead.

Without ``APP_PASSWORD`` set in production there is no way to prove you're the
owner, so the sync button simply isn't offered — the page still serves, and the
cron job still keeps it fresh.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

import pandas as pd
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from garmin_tracker import db, handoff, report, viz

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

APP_PASSWORD = os.getenv("APP_PASSWORD")
CRON_SECRET = os.getenv("CRON_SECRET")
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "7"))
# Activity-detail fetches allowed in one run. Each is a separate Garmin request
# for one session's streams, so this is the whole time budget of that endpoint.
# 25 sits comfortably inside 300s and clears a normal backlog in a day or two.
DETAILS_LIMIT = int(os.getenv("DETAILS_LIMIT", "25"))
IS_PROD = bool(os.getenv("VERCEL"))
# Reading the dashboard needs no password; set this to 0 to lock the page down
# to the owner as well.
PUBLIC_DASHBOARD = os.getenv("PUBLIC_DASHBOARD", "1").lower() not in ("0", "false", "no")
COOKIE = "progression_auth"


def _token() -> str:
    """Cookie value derived from the password — no session store needed."""
    return hmac.new((APP_PASSWORD or "").encode(), b"progression",
                    hashlib.sha256).hexdigest()


def _authed(cookie: str | None) -> bool:
    """Is this request from the owner?

    Gates the sync button and the JSON snapshot, never the page itself.
    """
    if not APP_PASSWORD:
        # No password to check against: trusted locally, nobody in production.
        return not IS_PROD
    return bool(cookie) and secrets.compare_digest(cookie, _token())


def _guard(cookie: str | None) -> None:
    if not _authed(cookie):
        raise HTTPException(status_code=401, detail="Not signed in.")


def _bearer_ok(request: Request) -> bool:
    """Machine-to-machine auth: `Authorization: Bearer <CRON_SECRET>`.

    Lets a trusted caller (e.g. the Que app the owner has connected this tracker
    to) trigger a sync and read the snapshot without the owner cookie. Same
    secret the scheduled cron uses; unset ⇒ no machine access.
    """
    if not CRON_SECRET:
        return False
    return secrets.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {CRON_SECRET}")


def _guard_owner_or_bearer(request: Request, cookie: str | None) -> None:
    if not (_authed(cookie) or _bearer_ok(request)):
        raise HTTPException(status_code=401, detail="Not signed in.")


# Shareable, but not searchable: a link you hand to a friend shouldn't turn up
# in results for your name.
_NOINDEX = "noindex, nofollow"


_LOGIN = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Progression</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Caprasimo&family=Figtree:wght@400;600;700&display=swap">
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:{viz.BG};color:{viz.INK};font-family:{viz.BODY_FONT}}}
form{{background:{viz.CARD};border:1px solid {viz.HAIR};border-radius:24px;
padding:32px;width:min(360px,92vw);display:flex;flex-direction:column;gap:14px}}
h1{{font-family:{viz.DISPLAY_FONT};font-size:26px;margin:0}}
p{{color:{viz.MUTED};font-size:13px;margin:0}}
input{{background:{viz.CARD_2};border:1px solid {viz.HAIR};border-radius:12px;
padding:11px 14px;color:{viz.INK};font-size:15px;font-family:inherit}}
button{{background:{viz.ACCENT};color:{viz.BG};border:none;border-radius:999px;
padding:11px;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit}}
.err{{color:{viz.STATUS["critical"]};font-size:13px}}
</style></head><body>
<form method="post" action="/login">
  <h1>Progression</h1><p>Ironman training tracker</p>
  {{error}}
  <input type="password" name="password" placeholder="Password" autofocus
         autocomplete="current-password">
  <button type="submit">Sign in</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form() -> HTMLResponse:
    note = ("" if APP_PASSWORD else
            '<div class="err">APP_PASSWORD is not set, so there is nothing to '
            'sign in to. Set it in the project settings to enable syncing.</div>')
    return HTMLResponse(_LOGIN.replace("{error}", note),
                        headers={"X-Robots-Tag": _NOINDEX})


@app.post("/login")
def login(password: str = Form("")) -> Response:
    if APP_PASSWORD and secrets.compare_digest(password, APP_PASSWORD):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, _token(), httponly=True, samesite="lax",
                        secure=IS_PROD, max_age=60 * 60 * 24 * 30)
        return resp
    return HTMLResponse(
        _LOGIN.replace("{error}", '<div class="err">Wrong password.</div>'),
        status_code=401, headers={"X-Robots-Tag": _NOINDEX})


@app.get("/logout")
def logout() -> Response:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request, weeks: int = 16,
          progression_auth: str | None = Cookie(None)) -> Response:
    owner = _authed(progression_auth)
    if not owner and not PUBLIC_DASHBOARD:
        return RedirectResponse("/login", status_code=303)

    # The sync button is the one owner-only thing on the page. Visitors get the
    # identical dashboard without it.
    html = report.render(weeks=max(1, min(weeks, 104)), show_sync=owner)

    # Rendering reads the database on every request. Visitors all see the same
    # page, so let the CDN absorb repeat views; the owner's copy carries a
    # button nobody else may use, so it must never be cached and handed out.
    cache = ("private, no-store" if owner else
             "public, max-age=0, s-maxage=60, stale-while-revalidate=300")
    return HTMLResponse(html, headers={
        "Cache-Control": cache, "X-Robots-Tag": _NOINDEX})


@app.get("/api/snapshot")
def snapshot(request: Request,
            progression_auth: str | None = Cookie(None)) -> JSONResponse:
    _guard_owner_or_bearer(request, progression_auth)
    return JSONResponse(handoff.build_snapshot())


def _run_sync(days: int) -> dict:
    """Shared by the button and the cron job.

    After pulling from Garmin, best-effort forward the new cardio to the owner's
    Que log if QUE_ACTIVITY_URL/TOKEN are set — so one sync updates both the
    tracker and Que. A missing Que config is silent; a push failure is reported
    in the summary but never fails the sync itself.
    """
    from garmin_tracker.sync import _no_prompt, run_sync

    db.init_db()
    summary = run_sync(days=days, mfa_prompt=_no_prompt)
    try:
        from garmin_tracker.que_push import QueNotConfigured, push_activities
        try:
            summary["que"] = push_activities(days=days)
        except QueNotConfigured:
            pass
    except Exception as e:  # noqa: BLE001 — never let a push break the sync
        summary["que_error"] = f"{type(e).__name__}: {e}"
    return summary


def _run_sync_details(limit: int) -> dict:
    """Fetch streams for long sessions and store their aerobic decoupling.

    Deliberately a separate pass from ``_run_sync``. The activity sync is a
    handful of requests; this is one request per eligible session, so bundling
    them would make the common sync as slow and as failure-prone as the rare
    one. Kept apart, each gets its own time budget and neither can take the
    other down.

    It is also self-limiting: only sessions with no decoupling value are
    fetched, newest first, so repeated runs walk backwards through the backlog
    and then do nothing.
    """
    from garmin_tracker.sync import _no_prompt, run_sync_details

    db.init_db()
    return run_sync_details(limit=limit, mfa_prompt=_no_prompt)


@app.post("/api/sync")
def sync(request: Request, days: int | None = None,
         progression_auth: str | None = Cookie(None)) -> JSONResponse:
    _guard_owner_or_bearer(request, progression_auth)
    from garmin_tracker.sync import MFARequired

    try:
        return JSONResponse(_run_sync(days or SYNC_DAYS))
    except MFARequired as e:
        # 409: the request was fine, the stored credentials just aren't usable
        # without a human. Seed tokens locally and re-upload.
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"{type(e).__name__}: {e}") from e


@app.post("/api/sync/details")
def sync_details(limit: int | None = None,
                 progression_auth: str | None = Cookie(None)) -> JSONResponse:
    """Backfill aerobic decoupling on demand, for clearing a backlog."""
    _guard(progression_auth)
    from garmin_tracker.sync import MFARequired

    try:
        return JSONResponse(_run_sync_details(limit or DETAILS_LIMIT))
    except MFARequired as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"{type(e).__name__}: {e}") from e


def _cron_guard(request: Request) -> None:
    """Vercel sends `Authorization: Bearer $CRON_SECRET` on scheduled calls."""
    if CRON_SECRET:
        if request.headers.get("authorization") != f"Bearer {CRON_SECRET}":
            raise HTTPException(status_code=401, detail="Bad cron secret.")
    elif IS_PROD:
        raise HTTPException(status_code=503, detail="CRON_SECRET is not set.")


def _cron_run(fn) -> JSONResponse:
    """Run a cron job without ever returning non-2xx on a Garmin failure.

    Vercel retries a failed cron, and a Garmin outage that retries is how you
    turn one bad morning into a rate limit. The failure goes in the body.
    """
    try:
        return JSONResponse(fn())
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=200)


@app.get("/api/cron/sync")
def cron_sync(request: Request) -> JSONResponse:
    _cron_guard(request)
    return _cron_run(lambda: _run_sync(SYNC_DAYS))


@app.get("/api/cron/sync-details")
def cron_sync_details(request: Request) -> JSONResponse:
    """Second daily job: the decoupling backfill.

    Separate from the activity cron so a slow stream fetch can't eat the time
    budget of the sync that actually keeps the page current.
    """
    _cron_guard(request)
    return _cron_run(lambda: _run_sync_details(DETAILS_LIMIT))


@app.get("/api/health")
def health() -> JSONResponse:
    counts, err, tokens = {}, None, None
    try:
        db.init_db()
        acts = db.load_activities()
        counts = {"activities": int(len(acts)),
                  "last_activity": (str(pd.to_datetime(acts["date"]).max().date())
                                    if not acts.empty else None)}
        # Inside the same guard as everything else: load_tokens now raises on an
        # unreachable database rather than reporting an empty store, and health
        # is the endpoint you check precisely when that is what's wrong.
        tokens = len(db.load_tokens())
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return JSONResponse({
        "ok": err is None,
        "storage": "postgres" if db.is_postgres() else "sqlite",
        "public_dashboard": PUBLIC_DASHBOARD,
        "password_set": bool(APP_PASSWORD),
        "sync_available": bool(APP_PASSWORD) or not IS_PROD,
        "cron_secret_set": bool(CRON_SECRET),
        "garmin_tokens": tokens,
        "counts": counts,
        "error": err,
    })
