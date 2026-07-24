#!/usr/bin/env python
"""Command-line entry point for the Garmin Ironman tracker.

Usage:
    python track.py sync                 # sync new data from the last 30 days
    python track.py sync --days 90       # widen the window
    python track.py sync --full          # re-fetch the window, ignoring what's stored
    python track.py sync --activities    # activities only (fast)
    python track.py sync --wellness      # daily wellness only
    python track.py sync-que             # pull strength workouts from Que
    python track.py backfill             # re-parse stored data into new columns (no network)
    python track.py info                 # show what's stored
    python track.py dashboard            # launch the Streamlit dashboard
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def cmd_sync(args) -> None:
    from garmin_tracker.sync import run_sync

    def progress(day: str) -> None:
        print(f"  fetching {day} ...", end="\r", flush=True)

    summary = run_sync(
        days=args.days,
        activities_only=args.activities,
        wellness_only=args.wellness,
        full=args.full,
        progress=progress,
    )
    print(
        f"Synced {summary['start']} -> {summary['end']}: "
        f"{summary['activities']} activities, "
        f"{summary['wellness_days']} wellness days fetched"
        f"{'' if args.full else ' (already-stored days skipped; --full to force)'}."
    )
    # Best-effort: also import strength from a Que export file if present.
    from garmin_tracker.config import settings
    if settings.que_export_path.exists():
        cmd_sync_que(args)


def cmd_sync_que(args) -> None:
    from garmin_tracker.que_sync import QueUnavailable, sync_strength

    try:
        s = sync_strength()
        span = f"{s['start']} -> {s['end']}" if s["start"] else "no lifts found"
        print(f"Que strength: {s['sessions']} sessions, {s['sets']} sets ({span}).")
    except QueUnavailable as e:
        print(f"Que strength: skipped — {e}")


def cmd_backfill(args) -> None:
    from garmin_tracker.sync import backfill_from_raw

    s = backfill_from_raw()
    print(f"Backfilled {s['updated']} activities from stored raw JSON "
          f"({s['skipped']} skipped). No Garmin requests were made.")


def cmd_tag(args) -> None:
    """Record your own read of a session: RPE and the talk test."""
    from datetime import datetime

    import pandas as pd

    from garmin_tracker import db

    db.init_db()
    acts = db.load_activities()
    tags = db.load_session_tags()
    if acts.empty:
        print("No activities yet - run `python track.py sync` first.")
        return

    tagged_ids = set(tags["activity_id"]) if not tags.empty else set()

    if args.list or not args.date:
        recent = acts.sort_values("date", ascending=False)
        if args.sport:
            recent = recent[recent["sport"] == args.sport]
        recent = recent.head(args.limit)
        print(f"Most recent {len(recent)} sessions "
              f"({len(tagged_ids)} of {len(acts)} tagged overall):\n")
        for _, r in recent.iterrows():
            mark = "*" if r["activity_id"] in tagged_ids else " "
            mins = (r["duration_s"] or 0) / 60
            print(f" {mark} {r['date']:%Y-%m-%d}  {r['sport']:<5} "
                  f"{mins:5.0f} min  {str(r['name'])[:34]}")
        print("\n* = already tagged")
        print("\nTag one with:")
        print("  python track.py tag 2026-07-21 --feel easy --rpe 3")
        print("\n--feel easy     = could hold a conversation / nose-breathe")
        print("  --feel moderate = short sentences only")
        print("  --feel hard     = a few words at a time")
        return

    day = pd.Timestamp(args.date).normalize()
    match = acts[acts["date"].dt.normalize() == day]
    if args.sport:
        match = match[match["sport"] == args.sport]
    if match.empty:
        print(f"No {args.sport or 'activities'} found on {day:%Y-%m-%d}. "
              "Run `python track.py tag --list` to see what's there.")
        return
    if len(match) > 1 and not args.all:
        print(f"{len(match)} sessions on {day:%Y-%m-%d}:")
        for _, r in match.iterrows():
            print(f"  {r['sport']:<5} {(r['duration_s'] or 0)/60:5.0f} min  {r['name']}")
        print("\nNarrow it with --sport run, or tag them all with --all.")
        return

    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        for _, r in match.iterrows():
            db.upsert_session_tag(conn, {
                "activity_id": int(r["activity_id"]),
                "date": day.strftime("%Y-%m-%d"),
                "rpe": args.rpe, "feel": args.feel, "note": args.note,
                "tagged_at": now,
            })
    bits = [b for b in (f"feel={args.feel}" if args.feel else None,
                        f"rpe={args.rpe}" if args.rpe is not None else None) if b]
    print(f"Tagged {len(match)} session(s) on {day:%Y-%m-%d}: {', '.join(bits) or 'note only'}")


def cmd_tokens(args) -> None:
    """Move the local Garmin token store into the database.

    A deployed instance has no writable disk and nobody to type an MFA code, so
    it reads its tokens from the database. Run this once, pointed at the
    deployment's database, after a successful local login.
    """
    from pathlib import Path as _Path

    from garmin_tracker import db
    from garmin_tracker.config import settings

    target = "Postgres" if db.is_postgres() else f"SQLite ({settings.db_path})"
    if args.check:
        stored = db.load_tokens()
        print(f"{target}: {len(stored)} token file(s) stored"
              + (f" — {', '.join(sorted(stored))}" if stored else ""))
        return

    store = _Path(settings.tokenstore)
    files = {p.name: p.read_text(encoding="utf-8")
             for p in store.glob("*") if p.is_file()}
    if not files:
        print(f"No token files in {store}. Run `python track.py sync` first "
              "and complete the Garmin login (including MFA).")
        return
    if not db.is_postgres():
        print("Warning: DATABASE_URL is not set, so these are going into the "
              "local SQLite file, not your deployment.")
    n = db.save_tokens(files)
    print(f"Pushed {n} token file(s) to {target}: {', '.join(sorted(files))}")


def cmd_report(args) -> None:
    """Render the deployable HTML page to a file and optionally open it."""
    import webbrowser
    from pathlib import Path as _Path

    from garmin_tracker.report import render

    out = _Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(weeks=args.weeks, show_sync=False), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    if not args.no_open:
        webbrowser.open(out.as_uri())


def cmd_handoff(args) -> None:
    from garmin_tracker.handoff import write_snapshot

    path = write_snapshot()
    print(f"Wrote design snapshot to {path}")
    print("Feed this plus design_handoff/DESIGN_BRIEF.md to your design tool.")


def cmd_info(args) -> None:
    from garmin_tracker import db

    db.init_db()
    acts = db.load_activities()
    daily = db.load_daily()
    sleep = db.load_sleep()

    print("=== Stored data ===")
    if acts.empty:
        print("Activities : none yet - run `python track.py sync` first.")
    else:
        by_sport = acts["sport"].value_counts().to_dict()
        span = f"{acts['date'].min().date()} -> {acts['date'].max().date()}"
        print(f"Activities : {len(acts)} ({span})")
        for sport, n in by_sport.items():
            print(f"             - {sport}: {n}")
    print(f"Wellness   : {len(daily)} days")
    print(f"Sleep      : {len(sleep)} nights")

    from garmin_tracker import metrics
    if not acts.empty:
        if metrics.has_zone_data(acts, "auto"):
            tid = metrics.intensity_summary(acts, source="auto")
            if tid:
                s = tid["shares"]
                print(f"Intensity  : {tid['label']} — "
                      f"{s['low']*100:.0f}% easy / {s['moderate']*100:.0f}% moderate / "
                      f"{s['high']*100:.0f}% hard, over {tid['hours']:.1f}h")
        else:
            print("Intensity  : no time-in-zone stored - run `python track.py backfill`.")

        hq = metrics.hr_quality_summary(acts)
        if hq and hq["suspect"]:
            print(f"HR quality : {hq['suspect']} of {hq['runs']} runs have an "
                  f"untrustworthy HR trace (scored from power instead)")
        cov = metrics.tag_coverage(acts, db.load_session_tags())
        print(f"Tagged     : {cov['tagged']} of {cov['total']} sessions have "
              f"an RPE / talk-test tag")

    strength = db.load_strength_sessions()
    if strength.empty:
        print("Strength   : none yet - run `python track.py sync-que` (needs Que configured).")
    else:
        span = f"{strength['date'].min().date()} -> {strength['date'].max().date()}"
        print(f"Strength   : {len(strength)} gym sessions ({span})")


def cmd_dashboard(args) -> None:
    app = Path(__file__).resolve().parent / "dashboard.py"
    try:
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(app)], check=True)
    except FileNotFoundError:
        print("Streamlit is not installed. Run: python -m pip install -r requirements.txt")


def main() -> None:
    parser = argparse.ArgumentParser(description="Garmin Ironman training tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Pull data from Garmin into the database")
    p_sync.add_argument("--days", type=int, default=30, help="Days of history to sync (default 30)")
    p_sync.add_argument("--activities", action="store_true", help="Sync activities only")
    p_sync.add_argument("--wellness", action="store_true", help="Sync daily wellness only")
    p_sync.add_argument("--full", action="store_true",
                        help="Re-fetch every day in the window instead of only missing ones")
    p_sync.set_defaults(func=cmd_sync)

    p_back = sub.add_parser(
        "backfill",
        help="Re-parse stored raw JSON into newly added columns (no network calls)")
    p_back.set_defaults(func=cmd_backfill)

    p_info = sub.add_parser("info", help="Show what is stored locally")
    p_info.set_defaults(func=cmd_info)

    p_tok = sub.add_parser(
        "tokens",
        help="Push the local Garmin token store into the database (for deploys)")
    p_tok.add_argument("--check", action="store_true",
                       help="Just report what's stored")
    p_tok.set_defaults(func=cmd_tokens)

    p_rep = sub.add_parser(
        "report", help="Render the static HTML dashboard (same page Vercel serves)")
    p_rep.add_argument("--out", default="build/index.html")
    p_rep.add_argument("--weeks", type=int, default=16)
    p_rep.add_argument("--no-open", action="store_true")
    p_rep.set_defaults(func=cmd_report)

    p_tag = sub.add_parser(
        "tag", help="Record RPE / talk-test feel for a session (no args = list recent)")
    p_tag.add_argument("date", nargs="?", help="Session date, YYYY-MM-DD")
    p_tag.add_argument("--feel", choices=["easy", "moderate", "hard"],
                       help="Talk test: easy = conversational / nose-breathing")
    p_tag.add_argument("--rpe", type=float, help="Session RPE, 1-10")
    p_tag.add_argument("--note", help="Free text")
    p_tag.add_argument("--sport", choices=["swim", "bike", "run", "other"])
    p_tag.add_argument("--all", action="store_true",
                       help="Tag every matching session that day")
    p_tag.add_argument("--list", action="store_true", help="List recent sessions")
    p_tag.add_argument("--limit", type=int, default=15)
    p_tag.set_defaults(func=cmd_tag)

    p_hand = sub.add_parser(
        "handoff",
        help="Regenerate design_handoff/sample_data.json from your real data")
    p_hand.set_defaults(func=cmd_handoff)

    p_que = sub.add_parser("sync-que", help="Pull strength workouts from the Que app")
    p_que.set_defaults(func=cmd_sync_que)

    p_dash = sub.add_parser("dashboard", help="Launch the Streamlit dashboard")
    p_dash.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
