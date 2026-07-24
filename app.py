"""Vercel entrypoint — the dashboard as a web service.

Routes
------
GET  /              the rendered dashboard
POST /api/sync      pull the latest data from Garmin, then reload
GET  /api/snapshot  the same numbers as JSON
GET  /api/cron/sync scheduled sync (Vercel Cron)
GET  /api/health    liveness + configuration check

Auth
----
Set ``APP_PASSWORD`` and every route asks for it once, storing a signed cookie.
Without it the app refuses to start in production — this page is your training
history and a button that spends your Garmin rate limit.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import date, timedelta

import pandas as pd
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from garmin_tracker import db, handoff, report, viz

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

APP_PASSWORD = os.getenv("APP_PASSWORD")
CRON_SECRET = os.getenv("CRON_SECRET")
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "7"))
IS_PROD = bool(os.getenv("VERCEL"))
COOKIE = "progression_auth"


def _token() -> str:
    """Cookie value derived from the password — no session store needed."""
    return hmac.new((APP_PASSWORD or "").encode(), b"progression",
                    hashlib.sha256).hexdigest()


def _authed(cookie: str | None) -> bool:
    if not APP_PASSWORD:
        # No password set: fine locally, refused in production.
        return not IS_PROD
    return bool(cookie) and secrets.compare_digest(cookie, _token())


def _guard(cookie: str | None) -> None:
    if not _authed(cookie):
        raise HTTPException(status_code=401, detail="Not signed in.")


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
    return HTMLResponse(_LOGIN.replace("{error}", ""))


@app.post("/login")
def login(password: str = Form("")) -> Response:
    if APP_PASSWORD and secrets.compare_digest(password, APP_PASSWORD):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, _token(), httponly=True, samesite="lax",
                        secure=IS_PROD, max_age=60 * 60 * 24 * 30)
        return resp
    return HTMLResponse(
        _LOGIN.replace("{error}", '<div class="err">Wrong password.</div>'),
        status_code=401)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, weeks: int = 16,
          progression_auth: str | None = Cookie(None)) -> Response:
    if not _authed(progression_auth):
        if not APP_PASSWORD and IS_PROD:
            return HTMLResponse(
                "<h1>APP_PASSWORD is not set</h1><p>Set it in the Vercel project "
                "settings before using this deployment — otherwise anyone with "
                "the URL can read your training data and spend your Garmin "
                "rate limit.</p>", status_code=503)
        return RedirectResponse("/login", status_code=303)
    html = report.render(weeks=max(1, min(weeks, 104)), show_sync=True)
    # Rendering reads the database on every request; let a shared cache absorb
    # repeat views while keeping the page fresh right after a sync.
    return HTMLResponse(html, headers={
        "Cache-Control": "private, max-age=0, s-maxage=60, "
                         "stale-while-revalidate=300"})


@app.get("/api/snapshot")
def snapshot(progression_auth: str | None = Cookie(None)) -> JSONResponse:
    _guard(progression_auth)
    return JSONResponse(handoff.build_snapshot())


def _run_sync(days: int) -> dict:
    """Shared by the button and the cron job."""
    from garmin_tracker.sync import _no_prompt, run_sync

    db.init_db()
    summary = run_sync(days=days, mfa_prompt=_no_prompt)
    return summary


@app.post("/api/sync")
def sync(days: int | None = None,
         progression_auth: str | None = Cookie(None)) -> JSONResponse:
    _guard(progression_auth)
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


@app.get("/api/cron/sync")
def cron_sync(request: Request) -> JSONResponse:
    """Vercel Cron target. Vercel sends `Authorization: Bearer $CRON_SECRET`."""
    if CRON_SECRET:
        if request.headers.get("authorization") != f"Bearer {CRON_SECRET}":
            raise HTTPException(status_code=401, detail="Bad cron secret.")
    elif IS_PROD:
        raise HTTPException(status_code=503, detail="CRON_SECRET is not set.")
    try:
        return JSONResponse(_run_sync(SYNC_DAYS))
    except Exception as e:
        # Never 500 a cron: a failed sync should be visible, not retried into
        # a rate limit.
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=200)


@app.get("/api/health")
def health() -> JSONResponse:
    counts, err = {}, None
    try:
        db.init_db()
        acts = db.load_activities()
        counts = {"activities": int(len(acts)),
                  "last_activity": (str(pd.to_datetime(acts["date"]).max().date())
                                    if not acts.empty else None)}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return JSONResponse({
        "ok": err is None,
        "storage": "postgres" if db.is_postgres() else "sqlite",
        "password_set": bool(APP_PASSWORD),
        "cron_secret_set": bool(CRON_SECRET),
        "garmin_tokens": len(db.load_tokens()),
        "counts": counts,
        "error": err,
    })
