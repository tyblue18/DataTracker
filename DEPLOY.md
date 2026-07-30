# Deploying to Vercel

The dashboard runs as a FastAPI app (`app.py`) that renders the page
server-side. Same numbers, same charts as the Streamlit app — but a serverless
function can return HTML, and it cannot host Streamlit's long-lived websocket
session, so `dashboard.py` stays a local tool and is excluded from the deploy.

**End state:** a public URL you can send to anyone, a **Sync now** button that
only *you* see, and a cron job that syncs twice a day so the page is fresh
before anyone opens it.

---

## Who can see what

| | Visitors | You |
|---|---|---|
| The dashboard | ✅ | ✅ |
| `Sync now` button | — | ✅ |
| `/api/snapshot` (raw JSON) | — | ✅ |
| Garmin credentials | — | — (env vars only) |

You are whoever knows `APP_PASSWORD`. Enter it once at `/login` on your phone
and the cookie lasts 30 days.

Reads are open and writes are not, because the sync button spends a finite
Garmin API rate limit — a page anyone can refresh is fine, a Garmin login
anyone can trigger is not.

The page is served with `X-Robots-Tag: noindex, nofollow`, so it's shareable
but won't turn up in a search for your name. That's not a secret: anyone with
the link can read it. Set `PUBLIC_DASHBOARD=0` to put the whole page behind the
password instead.

---

## Before you start

Two things must be true, and neither can be fixed after deploying:

1. **Your Garmin login must already work locally.** The first login needs an
   MFA code typed by a human, and there's nobody in a serverless function to
   type one. Run `python track.py sync` once and complete it.
2. **You need a Postgres database.** Vercel's filesystem is read-only —
   anything written to `data/garmin.db` is lost when the function ends.

## 1. Create the database

In the Vercel dashboard: **Storage → Create → Neon Postgres** (free tier is
ample — this is a few thousand rows). Vercel injects `DATABASE_URL` and
`POSTGRES_URL` into the project automatically.

Copy the connection string, then create the schema and move your history up:

```powershell
$env:DATABASE_URL = "postgres://...your neon url..."
python track.py sync --days 180 --full
```

`sync` re-pulls from Garmin straight into Postgres, which is simpler and more
reliable than migrating the SQLite file. Everything reads from whatever
`DATABASE_URL` points at, so unset it to go back to your local database.

## 2. Seed the Garmin tokens

```powershell
$env:DATABASE_URL = "postgres://..."
python track.py tokens            # pushes .garmintokens into the database
python track.py tokens --check    # confirm
```

Tokens refresh themselves on every sync and are written back automatically, so
this is a one-time step unless you change your Garmin password.

## 3. Set environment variables

In **Project → Settings → Environment Variables**:

| Variable | Value | Why |
|---|---|---|
| `APP_PASSWORD` | something long | Unlocks the sync button for you. Without it the page still serves — nobody can sync, including you. |
| `CRON_SECRET` | `openssl rand -hex 32` | Stops anyone triggering your cron endpoint. |
| `GARMIN_EMAIL` | your Garmin login | Fallback if tokens ever expire hard. |
| `GARMIN_PASSWORD` | your Garmin password | Same. |
| `GARMIN_RACE_DATE` | `2026-09-13` | Countdown + race projection. |
| `GARMIN_RACE_NAME` | `Ironman Wales` | |
| `GARMIN_WEEKLY_SESSIONS` | `6` | Consistency pillar target. |
| `SYNC_DAYS` | `7` | How far back each sync looks. |
| `PUBLIC_DASHBOARD` | *(omit)* | Defaults to public. Set `0` to require the password for the whole page. |

`DATABASE_URL` is set for you by the Neon integration.

## 4. Deploy

```bash
npm i -g vercel
vercel            # preview
vercel --prod
```

Then check it came up correctly:

```bash
curl https://your-app.vercel.app/api/health
```

```json
{"ok": true, "storage": "postgres", "public_dashboard": true,
 "password_set": true, "sync_available": true, "cron_secret_set": true,
 "garmin_tokens": 3, "counts": {"activities": 55}}
```

- `storage: "sqlite"` → `DATABASE_URL` isn't reaching the function; the page
  will look empty because it's reading a database that doesn't exist.
- `garmin_tokens: 0` → step 2 didn't land, and sync will fail with a 409.
- `sync_available: false` → `APP_PASSWORD` isn't set.

---

## How syncing works

- **Button** — *Sync now* in the header POSTs to `/api/sync`, pulls the last
  `SYNC_DAYS` days, and reloads. Days already stored are skipped, so a routine
  sync is a handful of requests and a few seconds. Only visible when signed in.
- **Cron** — `vercel.json` runs `/api/cron/sync` daily at 06:00 UTC, which is
  what keeps the page fresh for everyone else.

  **Hobby plans reject any cron that runs more than once a day** — a twice-daily
  expression fails at deploy time, it doesn't silently degrade. Timing is also
  only accurate to the hour (06:00 means somewhere in the 6am hour). Neither
  matters much here: each run pulls the last `SYNC_DAYS` days, so a missed or
  late run is picked up by the next one, and you can always hit the button.

Both share one code path with the CLI, so behaviour can't drift between them.

### What won't work remotely

- **A first-time Garmin login.** Needs MFA. `/api/sync` returns **409** with an
  explanation rather than hanging.
- **A long backfill.** Functions cap at 300s; a 90-day wellness pull is ~450
  sequential Garmin requests and will time out. Run big backfills locally with
  `DATABASE_URL` set.
- **Tagging sessions.** The manual override UI lives in the Streamlit app. The
  hosted page shows the sensor warning but is read-only.

## Local development

```bash
python -m pip install -r requirements-dev.txt

# The web service, exactly as deployed (sync enabled, no password needed)
python -m uvicorn app:app --reload --port 8000

# The static page, no server
python track.py report

# The optional Streamlit app, with session tagging
python track.py dashboard
```

Only `requirements.txt` is installed on Vercel. Streamlit, pytest and ruff live
in `requirements-dev.txt` so they stay out of the function bundle — Streamlit
alone pulls in pyarrow, which is the largest thing in the dependency tree and
is never imported by the deployed code path.

## Cost

Free tier throughout: Vercel Hobby, Neon free Postgres. Two cron runs a day and
personal traffic sit far inside the limits.

## Security notes

- Garmin credentials and tokens live in Vercel env vars and Postgres — never in
  the repo. `.env` and `.garmintokens/` are gitignored.
- The auth cookie is HTTP-only, `SameSite=Lax`, and `Secure` in production.
- `/api/health` deliberately exposes no training data, only whether the
  deployment is wired up.
- **Your training data is readable by anyone with the URL.** That's the
  intent — it's a page to show people. Don't treat it as private, and remember
  that GPS-bearing activity names can reveal where you live and train.
