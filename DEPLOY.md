# Deploying to Vercel

The dashboard runs as a FastAPI app (`app.py`) that renders the page
server-side. Same numbers, same charts as the Streamlit app — but a serverless
function can return HTML, and it cannot host Streamlit's long-lived websocket
session, so `dashboard.py` stays a local tool.

**End state:** a private URL you can open on your phone, a **Sync now** button
that pulls your latest workout on demand, and a cron job that syncs twice a day
so it's usually already fresh.

---

## Before you start

Two things must be true, and neither can be fixed after deploying:

1. **Your Garmin login must already work locally.** The first login needs an
   MFA code typed by a human, and there's nobody in a serverless function to
   type one. Run `python track.py sync` once and complete it.
2. **You need a Postgres database.** Vercel's filesystem is ephemeral —
   anything written to `data/garmin.db` disappears when the function ends.

---

## 1. Create the database

In the Vercel dashboard: **Storage → Create → Neon Postgres** (free tier is
ample — this is a few thousand rows). Vercel injects `DATABASE_URL` and
`POSTGRES_URL` into the project automatically.

Copy the connection string, then create the schema and move your history up:

```bash
# PowerShell
$env:DATABASE_URL = "postgres://...your neon url..."
python track.py backfill          # creates the schema in Postgres
```

> `backfill` reads from whatever `DATABASE_URL` points at. To copy an existing
> local SQLite database up, run `python track.py sync --days 180 --full` with
> `DATABASE_URL` set — it re-pulls from Garmin straight into Postgres, which is
> simpler and more reliable than migrating the file.

## 2. Seed the Garmin tokens

```bash
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
| `APP_PASSWORD` | something long | **Required.** The app refuses to serve without it — the URL is public otherwise. |
| `CRON_SECRET` | `openssl rand -hex 32` | Stops anyone triggering your cron endpoint. |
| `GARMIN_EMAIL` | your Garmin login | Fallback if tokens ever expire hard. |
| `GARMIN_PASSWORD` | your Garmin password | Same. |
| `GARMIN_RACE_DATE` | `2026-09-13` | Countdown + race projection. |
| `GARMIN_RACE_NAME` | `Ironman Wales` | |
| `GARMIN_WEEKLY_SESSIONS` | `6` | Consistency pillar target. |
| `SYNC_DAYS` | `7` | How far back each sync looks. |

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
{"ok": true, "storage": "postgres", "password_set": true,
 "cron_secret_set": true, "garmin_tokens": 3, "counts": {"activities": 55}}
```

If `storage` says `sqlite`, `DATABASE_URL` isn't reaching the function. If
`garmin_tokens` is `0`, step 2 didn't land — sync will fail with a 409.

---

## How syncing works

- **Button** — *Sync now* in the header POSTs to `/api/sync`, pulls the last
  `SYNC_DAYS` days, and reloads. Days already stored are skipped, so a routine
  sync is a handful of requests and a few seconds.
- **Cron** — `vercel.json` runs `/api/cron/sync` at 06:00 and 18:00 UTC. Adjust
  the schedule there.

Both share one code path with the CLI, so behaviour can't drift between them.

### What won't work remotely

- **A first-time Garmin login.** Needs MFA. `/api/sync` returns **409** with an
  explanation rather than hanging.
- **A long backfill.** Vercel functions cap at 300s; a 90-day wellness pull is
  ~450 sequential Garmin requests and will time out. Run big backfills locally
  with `DATABASE_URL` set.
- **Tagging sessions.** The manual override UI lives in the Streamlit app. The
  hosted page shows the sensor warning but is read-only.

---

## Local development

```bash
# The web service, exactly as deployed (no password needed off-Vercel)
python -m uvicorn app:app --reload --port 8000

# The static page, no server
python track.py report

# The full interactive app, with session tagging
python track.py dashboard
```

## Cost

Free tier throughout: Vercel Hobby, Neon free Postgres. Two cron runs a day and
personal traffic sit far inside the limits. Hobby has a 300s function ceiling,
which the sync fits in comfortably.

## Security notes

- `APP_PASSWORD` gates every route except `/api/health`. The cookie is
  HTTP-only, `SameSite=Lax`, and `Secure` in production.
- Garmin credentials and tokens live in Vercel env vars and Postgres — never in
  the repo. `.env` and `.garmintokens/` are gitignored.
- `/api/health` deliberately exposes no training data, only whether the
  deployment is wired up.
- Hobby projects can't use Vercel's built-in password protection, which is why
  this is done in the app.
