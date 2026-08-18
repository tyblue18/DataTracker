"""Tests for the deployed web service.

The Garmin sync itself is stubbed — these check the routing, the auth boundary
and the failure modes, which is where a deployment actually goes wrong.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# The page's script always references the button by id; only the element
# itself is conditional, so match the markup rather than the bare name.
SYNC_BUTTON = 'id="syncBtn"'


@pytest.fixture
def client(monkeypatch):
    """App with a known password, as it would run in production.

    Served over https because the auth cookie is marked Secure in production —
    an http test client would silently drop it, which is exactly what a plain
    http deployment would do too.
    """
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("CRON_SECRET", "cronsecret")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("PUBLIC_DASHBOARD", raising=False)  # exercise the default
    import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app, base_url="https://testserver"), app_module


@pytest.fixture
def open_client(monkeypatch):
    """No password, not production — the local-development case."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture
def locked_client(monkeypatch):
    """PUBLIC_DASHBOARD=0 — the whole page behind the password."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("PUBLIC_DASHBOARD", "0")
    import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app, base_url="https://testserver")


# --- Auth -------------------------------------------------------------------

def test_dashboard_is_public_without_signing_in(client):
    """The whole point of hosting it: a link a friend can open."""
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Progression" in r.text


def test_public_page_hides_the_sync_button(client):
    """Visitors must not be able to spend the Garmin rate limit."""
    c, _ = client
    assert SYNC_BUTTON not in c.get("/").text
    c.post("/login", data={"password": "hunter2"})
    assert SYNC_BUTTON in c.get("/").text


def test_public_page_offers_the_way_to_the_sync_button(client):
    """Hiding the button is right; hiding /login too leaves no way in."""
    c, _ = client
    assert 'href="/login"' in c.get("/").text
    c.post("/login", data={"password": "hunter2"})
    assert 'href="/login"' not in c.get("/").text


def test_public_page_is_cacheable_and_the_owners_is_not(client):
    c, _ = client
    assert "public" in c.get("/").headers["cache-control"]
    c.post("/login", data={"password": "hunter2"})
    assert "no-store" in c.get("/").headers["cache-control"]


def test_page_varies_on_cookie(client):
    """Without this the CDN answers the owner from the visitor's cached copy,
    the function never runs, and the sync button can never appear."""
    c, _ = client
    assert c.get("/").headers["vary"] == "Cookie"
    c.post("/login", data={"password": "hunter2"})
    assert c.get("/").headers["vary"] == "Cookie"


def test_page_is_not_search_indexable(client):
    c, _ = client
    assert c.get("/").headers["x-robots-tag"] == "noindex, nofollow"


def test_login_with_correct_password_sets_a_cookie(client):
    c, mod = client
    r = c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    assert r.status_code == 303
    assert mod.COOKIE in r.cookies


def test_login_rejects_a_wrong_password(client):
    c, _ = client
    r = c.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    assert "Wrong password" in r.text


def test_logout_clears_the_cookie(client):
    c, _ = client
    c.post("/login", data={"password": "hunter2"})
    assert SYNC_BUTTON in c.get("/").text
    c.get("/logout")
    assert SYNC_BUTTON not in c.get("/").text


def test_api_requires_auth(client):
    c, _ = client
    assert c.post("/api/sync").status_code == 401
    assert c.get("/api/snapshot").status_code == 401


def test_production_without_a_password_serves_but_disables_sync(monkeypatch):
    """No password means nobody can prove ownership — reads stay open."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("PUBLIC_DASHBOARD", raising=False)
    import app as app_module
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    assert c.get("/").status_code == 200
    assert SYNC_BUTTON not in c.get("/").text
    assert c.post("/api/sync").status_code == 401
    assert c.get("/api/health").json()["sync_available"] is False


def test_public_dashboard_can_be_turned_off(locked_client):
    r = locked_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_local_use_needs_no_password(open_client):
    assert open_client.get("/").status_code == 200


# --- Page -------------------------------------------------------------------

def test_page_renders_after_login(client):
    c, _ = client
    c.post("/login", data={"password": "hunter2"})
    r = c.get("/")
    assert r.status_code == 200
    assert "Progression" in r.text
    assert "<svg" in r.text


def test_weeks_parameter_is_clamped(client, monkeypatch):
    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    seen = {}

    def fake_render(weeks, show_sync, sign_in_url):
        seen["weeks"] = weeks
        return "<html>ok</html>"

    monkeypatch.setattr(mod.report, "render", fake_render)
    c.get("/?weeks=9999")
    assert seen["weeks"] == 104
    c.get("/?weeks=-5")
    assert seen["weeks"] == 1


# --- Sync -------------------------------------------------------------------

def test_sync_returns_the_summary(client, monkeypatch):
    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    monkeypatch.setattr(mod, "_run_sync",
                        lambda days, resend=False: {"activities": 3, "wellness_days": 7})
    r = c.post("/api/sync")
    assert r.status_code == 200
    assert r.json()["activities"] == 3


def test_sync_reports_mfa_as_409_not_500(client, monkeypatch):
    """A 2FA prompt isn't a server fault — the UI needs to say what to do."""
    from garmin_tracker.sync import MFARequired

    c, mod = client
    c.post("/login", data={"password": "hunter2"})

    def boom(days, resend=False):
        raise MFARequired("needs a code")

    monkeypatch.setattr(mod, "_run_sync", boom)
    r = c.post("/api/sync")
    assert r.status_code == 409
    assert "needs a code" in r.json()["detail"]


def test_sync_reports_garmin_failure_as_502(client, monkeypatch):
    c, mod = client
    c.post("/login", data={"password": "hunter2"})

    def boom(days):
        raise ConnectionError("garmin down")

    monkeypatch.setattr(mod, "_run_sync", boom)
    assert c.post("/api/sync").status_code == 502


# --- Cron -------------------------------------------------------------------

def test_cron_requires_the_secret(client):
    c, _ = client
    assert c.get("/api/cron/sync").status_code == 401


def test_cron_runs_with_the_secret(client, monkeypatch):
    c, mod = client
    monkeypatch.setattr(mod, "_run_sync",
                        lambda days: {"activities": 1, "wellness_days": 1})
    r = c.get("/api/cron/sync",
              headers={"Authorization": "Bearer cronsecret"})
    assert r.status_code == 200
    assert r.json()["activities"] == 1


def test_cron_failure_does_not_500(client, monkeypatch):
    """Vercel retries non-2xx crons; a Garmin outage must not become a retry storm."""
    c, mod = client

    def boom(days):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(mod, "_run_sync", boom)
    r = c.get("/api/cron/sync", headers={"Authorization": "Bearer cronsecret"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# --- Health -----------------------------------------------------------------

def test_health_is_public_and_reports_config(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["password_set"] is True
    assert body["cron_secret_set"] is True
    assert body["storage"] in ("sqlite", "postgres")


def test_health_reports_whether_the_que_push_is_configured(client, monkeypatch):
    """A deployment without the Que env vars syncs fine but forwards nothing —
    health is where that has to show up, or it looks like a broken button."""
    from types import SimpleNamespace

    c, mod = client
    monkeypatch.setattr(mod, "settings", SimpleNamespace(
        que_activity_url="https://que.example/api/health/activity",
        que_activity_token="tok"))
    assert c.get("/api/health").json()["que_push_configured"] is True

    monkeypatch.setattr(mod, "settings", SimpleNamespace(
        que_activity_url=None, que_activity_token=None))
    assert c.get("/api/health").json()["que_push_configured"] is False


def test_sync_summary_names_a_missing_que_config(client, monkeypatch):
    """QueNotConfigured must not be silent — the summary says what to set."""
    import garmin_tracker.que_push as que_mod
    import garmin_tracker.sync as sync_mod

    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    monkeypatch.setattr(mod.db, "init_db", lambda: None)
    monkeypatch.setattr(sync_mod, "run_sync",
                        lambda days, mfa_prompt: {"activities": 1,
                                                  "new_activities": 1,
                                                  "wellness_days": 0})

    def not_configured(days, resend=False):
        raise que_mod.QueNotConfigured("unset")

    monkeypatch.setattr(que_mod, "push_activities", not_configured)
    body = c.post("/api/sync").json()
    assert "QUE_ACTIVITY_URL" in body["que"]


# --- Details sync (aerobic decoupling backfill) ------------------------------

def test_details_sync_requires_auth(client):
    c, _ = client
    assert c.post("/api/sync/details").status_code == 401


def test_details_sync_returns_the_summary(client, monkeypatch):
    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    monkeypatch.setattr(mod, "_run_sync_details",
                        lambda limit: {"eligible": 9, "fetched": 9, "computed": 7})
    r = c.post("/api/sync/details")
    assert r.status_code == 200
    assert r.json()["computed"] == 7


def test_details_sync_is_always_bounded(client, monkeypatch):
    """One Garmin request per session — an unbounded run would hit the 300s cap."""
    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    seen = []
    monkeypatch.setattr(mod, "_run_sync_details",
                        lambda limit: (seen.append(limit), {})[1])

    c.post("/api/sync/details")
    c.post("/api/sync/details?limit=3")

    assert seen == [mod.DETAILS_LIMIT, 3]
    assert all(isinstance(n, int) and n > 0 for n in seen)


def test_details_sync_reports_mfa_as_409(client, monkeypatch):
    from garmin_tracker.sync import MFARequired

    c, mod = client
    c.post("/login", data={"password": "hunter2"})

    def boom(limit):
        raise MFARequired("needs a code")

    monkeypatch.setattr(mod, "_run_sync_details", boom)
    assert c.post("/api/sync/details").status_code == 409


def test_details_cron_requires_the_secret(client):
    c, _ = client
    assert c.get("/api/cron/sync-details").status_code == 401


def test_details_cron_runs_with_the_secret(client, monkeypatch):
    c, mod = client
    monkeypatch.setattr(mod, "_run_sync_details",
                        lambda limit: {"eligible": 2, "fetched": 2, "computed": 2})
    r = c.get("/api/cron/sync-details",
              headers={"Authorization": "Bearer cronsecret"})
    assert r.status_code == 200
    assert r.json()["computed"] == 2


def test_details_cron_failure_does_not_500(client, monkeypatch):
    """Same rule as the activity cron: a Garmin outage must not trigger retries."""
    c, mod = client

    def boom(limit):
        raise RuntimeError("garmin down")

    monkeypatch.setattr(mod, "_run_sync_details", boom)
    r = c.get("/api/cron/sync-details",
              headers={"Authorization": "Bearer cronsecret"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_details_sync_does_not_run_inside_the_activity_sync(client, monkeypatch):
    """They are separate passes on purpose — bundling makes the common sync slow."""
    c, mod = client
    c.post("/login", data={"password": "hunter2"})
    called = []
    monkeypatch.setattr(mod, "_run_sync_details",
                        lambda limit: called.append(limit) or {})
    monkeypatch.setattr(mod, "_run_sync", lambda days: {"activities": 1})
    c.post("/api/sync")
    assert called == []


def test_health_reports_a_broken_database_rather_than_zero_tokens(client, monkeypatch):
    """The endpoint you check when the database is the thing that's wrong."""
    c, mod = client

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mod.db, "load_tokens", boom)
    body = c.get("/api/health").json()
    assert body["ok"] is False
    assert body["garmin_tokens"] is None      # not 0 — we don't know
    assert "connection refused" in body["error"]


# --- Que config handover ------------------------------------------------------

def test_que_config_requires_the_bearer_secret(client):
    c, _ = client
    r = c.post("/api/que-config", json={"activityUrl": "https://q.app/api/health/activity", "token": "t"})
    assert r.status_code == 401


def test_que_config_stores_the_handed_over_credentials(client, monkeypatch):
    """The endpoint persists via db.set_config — stubbed so the test never
    touches a real database file."""
    c, _ = client
    stored = {}
    from garmin_tracker import db as _db
    monkeypatch.setattr(_db, "set_config", lambda k, v, db_path=None: stored.__setitem__(k, v))
    r = c.post("/api/que-config",
               headers={"Authorization": "Bearer cronsecret"},
               json={"activityUrl": "https://q.app/api/health/activity", "token": "tok123"})
    assert r.status_code == 200
    assert stored == {"que_activity_url": "https://q.app/api/health/activity",
                      "que_activity_token": "tok123"}


def test_que_config_rejects_a_non_que_url(client):
    c, _ = client
    r = c.post("/api/que-config",
               headers={"Authorization": "Bearer cronsecret"},
               json={"activityUrl": "https://evil.example/steal", "token": "t"})
    assert r.status_code == 400
