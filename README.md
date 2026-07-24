# 🏊🚴🏃 Garmin Ironman Training Tracker

[![CI](https://github.com/tyblue18/DataTracker/actions/workflows/ci.yml/badge.svg)](https://github.com/tyblue18/DataTracker/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Sync your Garmin Connect data into a local database, then track the trends that
actually matter for Ironman: **training load (fitness/fatigue/form)**, **weekly
swim/bike/run volume**, **resting HR & HRV**, **VO₂max**, **run efficiency**, and
**sleep** — all building up over months of training.

This is a **standalone app** — it talks to Garmin Connect directly through the
`garminconnect` library and needs nothing else to run.

> If you ever want to *chat* with Claude about your data on top of this, the
> [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) MCP server is one
> option (it exposes read-only Garmin queries to an LLM but stores no history).
> It's entirely optional and not required by this tracker — see the note at the
> bottom.

---

## Quick start

```bash
# 1. Install dependencies
python -m pip install -r requirements.txt

# 2. Add your Garmin credentials (and your race date)
cp .env.example .env        # then edit .env

# 3. Pull your history (last 90 days, first run handles MFA)
python track.py sync --days 90

# 4. Extract everything from what you already downloaded (no network calls)
python track.py backfill

# 5. See what's stored
python track.py info

# 6. Launch the dashboard
python track.py dashboard
```

`backfill` re-parses the raw Garmin payload already saved in the database, so
new columns (time-in-zone, running dynamics, normalised power) get filled in
for activities synced by older versions. It never touches Garmin.

On the **first sync** you'll be prompted for the Garmin MFA code if your account
uses two-factor auth. After that, OAuth tokens are cached in `.garmintokens/`
(gitignored) and you won't need to log in again.

---

## How it works

```
Garmin Connect ──(garminconnect)──▶ sync.py ──▶ SQLite (garmin.db)
                                                     │
                                          metrics.py │ (pandas trends)
                                                     ▼
                                              dashboard.py (Streamlit)
```

| File | Role |
|------|------|
| `garmin_tracker/client.py`  | Authenticated Garmin session (token cache + MFA) |
| `garmin_tracker/db.py`      | SQLite schema, migrations + idempotent upserts |
| `garmin_tracker/sync.py`    | Pull activities + daily wellness, parse defensively |
| `garmin_tracker/metrics.py` | CTL/ATL/TSB, intensity distribution, HRV bands, trends |
| `garmin_tracker/viz.py`     | Design tokens (palette, type, status colours) |
| `garmin_tracker/charts.py`  | Hand-built SVG charts |
| `garmin_tracker/handoff.py` | Real-data export for design work |
| `dashboard.py`              | Streamlit dashboard |
| `track.py`                  | CLI: `sync`, `backfill`, `tag`, `sync-que`, `info`, `handoff`, `dashboard` |
| `tests/`                    | `pytest` suite over the metrics layer |

### What gets stored

- **activities** — every swim/bike/run/other: distance, duration, HR, pace,
  power, elevation, calories, Garmin training load, training effect,
  **seconds in each HR and power zone**, normalised power, and running
  dynamics (cadence, stride, ground contact, vertical ratio).
- **daily_metrics** — resting HR, overnight HRV, stress, body battery, steps,
  VO₂max (run + bike), training status, weight.
- **sleep** — total/deep/light/REM/awake time and sleep score.

Re-syncing an overlapping date range is safe — upserts refresh values instead of
creating duplicates, and days already stored are skipped (`--full` forces a
re-fetch). The full raw Garmin JSON is kept in a `raw` column so nothing is ever
lost, and `backfill` can replay it into new columns later.

---

## What it measures

### Fitness · Fatigue · Form

The standard training-stress model, driven by Garmin's per-activity load:

- **CTL — Fitness** (42-day weighted load): your built-up endurance base.
- **ATL — Fatigue** (7-day weighted load): recent tiredness.
- **TSB — Form** (Fitness − Fatigue): positive = fresh/tapered, deeply negative =
  overreaching. Aim for roughly +5 to +25 on race day.

Both averages are carried forward through rest days and computed **as of today**,
so a layoff shows as the decline it is rather than freezing at your last workout.
The first 42 days are marked as model warm-up, because CTL climbing out of its
seed is not the same thing as fitness being built.

### Training-intensity distribution

How your time splits below / at / above threshold, from per-activity time-in-zone.
The dashboard puts your split next to the two models that hold up in the
literature — polarised (~80/5/15) and pyramidal (~78/19/3) — both of which put
roughly three-quarters of training time in the easy band. A split with more hard
time than easy is flagged as inverted.

### Heart-rate trust (optical cadence lock)

Wrist optical sensors fail in a specific way during running: they lock onto the
motion artefact from foot strike and report **cadence as heart rate**. It doesn't
look like an error — it looks like a plausible 170 bpm — so every downstream
metric absorbs it silently.

Each run gets two checks: whether HR is sitting on top of cadence, and whether HR
and running power disagree about how hard the session was. The second is the one
that convicts, because power is derived from pace and motion rather than from the
optical sensor. Runs that fail are scored from **power zones** instead, and are
dropped from run-efficiency trends entirely (metres per heartbeat means nothing
if the heartbeat is cadence).

If you'd rather not buy a strap, the manual override below does the same job by
hand — and a tag beats both sensors outright.

### Your own read of a session — the manual override

A sensor is inferring; you were there. On the **Intensity** tab, *Correct a
session* lists the runs the sensor called hard, and one click relabels one:

- **easy** — you could hold a conversation and breathe through your nose
- **moderate** — short sentences only
- **hard** — a few words at a time

That's the talk test, a validated field surrogate for the first ventilatory
threshold. A tag **overrides both sensors** on the Auto source: the session's
measured duration is kept and only its placement in the zones changes, per
`metrics.FEEL_TO_ZONE`. It is the one intensity signal no hardware can corrupt,
and it needs no chest strap.

Same thing from the CLI:

```bash
python track.py tag --list                              # recent sessions
python track.py tag 2026-07-21 --feel easy --rpe 3      # tag one
```

RPE is optional; where you give it, `RPE × minutes` (session-RPE) is the
best-validated training-load measure there is. The dashboard also shows where
your read and the sensor disagree.

### HRV against your own normal range

Not the daily number, which is mostly noise. A 7-day rolling mean of ln(HRV)
against a 60-day baseline, with a normal range of ±0.5 SD (the smallest
worthwhile change) — the construction used in HRV-guided training studies.

### Progression Score

One 0–100 index over five pillars: Fitness, Efficiency, Recovery, Consistency and
Intensity. Pillars reward trends against your own baseline, not absolute values.
A pillar that can't be computed shows “—” rather than a guess, and a pillar never
collapses onto a single input — so "recovery is strong" can't be produced by
simply not training.

---

## Keep it up to date

Run `python track.py sync` after workouts, or schedule it. On Windows you can use
Task Scheduler to run a daily sync, e.g.:

```powershell
schtasks /create /tn "GarminSync" /tr "python C:\Users\tanis\OneDrive\Desktop\Data_tracker\track.py sync" /sc daily /st 21:00
```

---

## Using the Garmin MCP alongside this (optional)

To *chat* with Claude about your data, add the MCP server too:

```bash
git clone https://github.com/Taxuspt/garmin_mcp
# follow that repo's README to register it with your Claude client
```

Then you can ask things like *"how did my long runs trend this month?"* while this
tracker keeps the durable history and dashboards.

---

## Notes & limits

- Training load comes from Garmin's EPOC-based model, not TSS. It is not directly
  comparable to a TrainingPeaks number, and it is not strictly comparable *across
  sports* either — an hour of swimming and an hour of running at the same
  perceived effort produce different values. CTL/ATL are still the right shape;
  just don't read the absolute number as a TSS.
- Activities without a load value contribute 0 to CTL/ATL (older devices may not
  report it).
- HR zones come from whatever zone setup your Garmin account has. If those
  boundaries are wrong, the intensity distribution is wrong in the same
  direction — worth checking them in Garmin Connect once.
- Wearable VO₂max is an estimate that moves far more slowly than real fitness;
  it's shown as a stat chip, not a trend.
- Garmin's unofficial API can change shape; parsing is defensive and the raw JSON
  is retained so you can recover any field later.
- All data stays **local** — nothing leaves your machine except the Garmin login.

## Access it anywhere

`app.py` serves the same dashboard as a web service — see **[DEPLOY.md](DEPLOY.md)**
for the Vercel walkthrough. You get a private URL, a **Sync now** button, and a
cron job that pulls new workouts twice a day.

```bash
python -m uvicorn app:app --reload --port 8000   # the hosted app, locally
python track.py report                           # or just render the page
```

Streamlit can't run on Vercel (it needs a long-lived websocket server), so the
hosted page is server-rendered and read-only; `track.py dashboard` stays the
local tool for tagging sessions.

## Tests

```bash
python -m pytest tests/ -q
```
