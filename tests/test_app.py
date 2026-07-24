"""Tests for the deployed web service.

The Garmin sync itself is stubbed — these check the routing, the auth boundary
and the failure modes, which is where a deployment actually goes wrong.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


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


# --- Auth -------------------------------------------------------------------

def test_index_redirects_to_login_when_signed_out(client):
    c, _ = client
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


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


def test_api_requires_auth(client):
    c, _ = client
    assert c.post("/api/sync").status_code == 401
    assert c.get("/api/snapshot").status_code == 401


def test_production_without_a_password_refuses_to_serve(monkeypatch):
    """A public URL with no password would expose everything — fail closed."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    import app as app_module
    importlib.reload(app_module)
    r = TestClient(app_module.app).get("/")
    assert r.status_code == 503
    assert "APP_PASSWORD" in r.text


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

    def fake_render(weeks, show_sync):
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
                        lambda days: {"activities": 3, "wellness_days": 7})
    r = c.post("/api/sync")
    assert r.status_code == 200
    assert r.json()["activities"] == 3


def test_sync_reports_mfa_as_409_not_500(client, monkeypatch):
    """A 2FA prompt isn't a server fault — the UI needs to say what to do."""
    from garmin_tracker.sync import MFARequired

    c, mod = client
    c.post("/login", data={"password": "hunter2"})

    def boom(days):
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
