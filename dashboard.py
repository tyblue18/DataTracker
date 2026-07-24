"""Progression — Ironman training dashboard.

Run with:  python track.py dashboard
    (or:    python -m streamlit run dashboard.py)

Reading order is deliberate: what needs attention, then the one headline score,
then the evidence behind it. Every number is computed as of *today*, not as of
the last workout, so time off reads as time off.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import streamlit as st

from garmin_tracker import charts, db, metrics, viz
from garmin_tracker.config import settings

st.set_page_config(page_title="Progression · Ironman Tracker",
                   page_icon="🏊", layout="wide")

TODAY = pd.Timestamp(date.today())

# One unbroken HTML block: Streamlit renders through markdown, and markdown ends
# a raw HTML block at the first blank line — a blank line inside <style> would
# dump the rest of the stylesheet onto the page as text.
_CSS = f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Caprasimo&family=Figtree:wght@400;500;600;700;800&display=swap">
<style>
.stApp {{ background:{viz.BG}; }}
html, body, [class*="css"] {{ font-family:{viz.BODY_FONT}; color:{viz.INK}; }}
.block-container {{ padding-top:3.4rem; padding-bottom:4rem; max-width:1300px; }}
#MainMenu, footer {{ visibility:hidden; }}
.card {{ background:{viz.CARD}; border:1px solid {viz.HAIR}; border-radius:24px; padding:24px; height:100%; }}
.card-sm {{ background:{viz.CARD}; border:1px solid {viz.HAIR}; border-radius:20px; padding:18px; height:100%; }}
.kicker {{ font-size:11.5px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:{viz.MUTED}; }}
.display {{ font-family:{viz.DISPLAY_FONT}; line-height:.9; }}
.h-sec {{ font-family:{viz.DISPLAY_FONT}; font-size:26px; color:{viz.INK}; margin-bottom:2px; }}
.sub {{ font-size:14px; color:{viz.MUTED}; }}
.pill {{ font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; padding:3px 11px; border-radius:999px; display:inline-block; }}
.muted {{ color:{viz.MUTED}; font-size:12px; line-height:1.45; }}
.flag {{ display:flex; align-items:flex-start; gap:14px; background:{viz.CARD}; border:1px solid {viz.HAIR}; border-radius:18px; padding:13px 18px; margin-bottom:9px; }}
.flag-dot {{ width:34px; height:34px; flex:none; border-radius:999px; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; }}
.track {{ height:6px; border-radius:99px; background:{viz.GRID_2}; margin-top:12px; overflow:hidden; }}
.fill {{ height:6px; border-radius:99px; }}
.stack {{ display:flex; border-radius:99px; overflow:hidden; }}
.stack > div {{ display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700; }}
div[data-testid="stSidebar"] {{ background:{viz.CARD}; }}
div[data-baseweb="select"] > div {{ background:{viz.CARD_2}; border-color:{viz.HAIR}; }}
.stButton > button {{ background:{viz.ACCENT}; color:{viz.BG}; border:none; border-radius:999px; font-weight:700; padding:.4rem 1.1rem; }}
.stButton > button:hover {{ background:{viz.ACCENT_2}; color:{viz.BG}; }}
div[data-testid="stExpander"] {{ background:{viz.CARD}; border:1px solid {viz.HAIR}; border-radius:20px; }}
div[data-testid="stTabs"] button[role="tab"] {{ font-weight:600; }}
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


@st.cache_data(ttl=300)
def _load():
    db.init_db()
    return (db.load_activities(), db.load_daily(), db.load_sleep(),
            db.load_strength_sessions(), db.load_strength_sets(),
            db.load_session_tags())


(activities, daily, sleep, strength_sessions, strength_sets,
 session_tags) = _load()


def md(s: str) -> None:
    st.markdown(s, unsafe_allow_html=True)


def pill(text: str, color: str) -> str:
    return (f'<span class="pill" style="color:{color};'
            f'background:{viz.tint(color)}">{text}</span>')


# =============================================================================
# Header
# =============================================================================
if activities.empty and daily.empty:
    md('<div class="h-sec">Progression</div>')
    st.warning("No data yet. Run `python track.py sync` to pull your Garmin "
               "history, then reload.")
    st.stop()

stale = None
last_any = max([pd.to_datetime(df["date"]).max() for df in (activities, daily)
                if not df.empty], default=pd.NaT)
if pd.notna(last_any):
    stale = int((TODAY - last_any).days)
sync_color = viz.STATUS["good"] if stale == 0 else viz.STATUS["warning"]
sync_label = ("Synced today" if stale == 0 else
              f"{stale} days stale — figures read as time off")

race_html = ""
if settings.race_date:
    try:
        rd = datetime.strptime(settings.race_date, "%Y-%m-%d").date()
        days_to = (rd - date.today()).days
        if days_to >= 0:
            race_html = (
                f'<div style="display:flex;align-items:center;gap:8px;'
                f'background:{viz.tint(viz.ACCENT)};border:1px solid '
                f'{viz.tint(viz.ACCENT, .35)};border-radius:999px;padding:7px 14px;'
                f'font-size:12.5px;color:{viz.ACCENT_2};font-weight:600">'
                f'⏱ {days_to} days to {settings.race_name}</div>')
    except ValueError:
        race_html = '<div class="muted">Bad GARMIN_RACE_DATE (use YYYY-MM-DD)</div>'
else:
    race_html = (f'<div style="border:1px dashed {viz.tint(viz.INK, .25)};'
                 f'border-radius:999px;padding:7px 14px;font-size:12.5px;'
                 f'color:{viz.MUTED}">+ Set race day in .env</div>')

md(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
            gap:16px;padding:4px 0 14px;flex-wrap:wrap">
  <div style="display:flex;align-items:center;gap:12px">
    <div style="width:34px;height:34px;border-radius:999px;background:{viz.ACCENT};
                display:flex;align-items:center;justify-content:center">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{viz.BG}"
           stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
    </div>
    <div>
      <div class="display" style="font-size:22px">Progression</div>
      <div style="font-size:12px;color:{viz.MUTED};margin-top:2px">
        Ironman training · Garmin data</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:7px;background:{viz.CARD};
                border:1px solid {viz.HAIR};border-radius:999px;padding:7px 14px;
                font-size:12.5px;color:{viz.MUTED_2}">
      <span style="width:7px;height:7px;border-radius:99px;
                   background:{sync_color};display:inline-block"></span>{sync_label}
    </div>{race_html}
  </div>
</div>
""")

# --- Controls ---------------------------------------------------------------
with st.sidebar:
    md('<div class="kicker">Filters</div>')
    weeks = st.slider("Weeks of history", 4, 52, 16)
    cutoff = TODAY - pd.Timedelta(weeks=weeks)
    st.caption("Charts respect this window. Scores and baselines always use full "
               "history — a 60-day baseline can't come from a 4-week window.")
    philosophy = st.selectbox("Scoring philosophy",
                              ["balanced", "performance", "health"])
    zone_source = st.selectbox(
        "Intensity measured from", ["auto", "hr", "power"],
        format_func={"auto": "Auto (recommended)", "hr": "Heart rate",
                     "power": "Power"}.get,
        help="Auto trusts your own session tags first, then heart rate, falling "
             "back to power on runs whose HR trace fails the quality check.")
    st.write("")
    md('<div class="kicker">Sync</div>')
    sync_days = st.number_input("Days to pull", 1, 120, 7, step=1,
                                help="Days already stored are skipped, so a "
                                     "routine sync is only a few requests.")
    if st.button("⟳ Sync from Garmin", width="stretch"):
        from garmin_tracker.sync import MFARequired, _no_prompt, run_sync
        status = st.status("Contacting Garmin…", expanded=True)
        try:
            summary = run_sync(days=int(sync_days), mfa_prompt=_no_prompt,
                               progress=lambda d: status.update(
                                   label=f"Fetching {d}…"))
        except MFARequired as e:
            status.update(label="Two-factor code needed", state="error")
            st.error(str(e))
        except Exception as e:  # network, Garmin outage, API drift
            status.update(label="Sync failed", state="error")
            st.error(f"{type(e).__name__}: {e}")
        else:
            # New activities arrive with their zone data already parsed; the
            # backfill only matters for rows synced by older versions.
            status.update(
                label=f"Synced {summary['activities']} activities · "
                      f"{summary['wellness_days']} wellness days",
                state="complete")
            st.cache_data.clear()
            st.rerun()

    if st.button("↻ Reload from database", width="stretch"):
        st.cache_data.clear()
        st.rerun()

acts = activities[activities["date"] >= cutoff] if not activities.empty else activities
day = daily[daily["date"] >= cutoff] if not daily.empty else daily
slp = sleep[sleep["date"] >= cutoff] if not sleep.empty else sleep

# =============================================================================
# Flags
# =============================================================================
flags = metrics.training_flags(activities, daily, sleep, through=TODAY,
                               tags=session_tags)
for f in flags[:5]:
    c = viz.status_color(f["status"])
    md(f"""<div class="flag">
      <div class="flag-dot" style="background:{viz.tint(c)};color:{c}">!</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="font-weight:700;font-size:14px">{f['title']}</span>
          {pill(f['status'], c)}
        </div>
        <div style="font-size:13px;color:{viz.MUTED_2};margin-top:3px">
          {f['detail']}</div>
      </div></div>""")

tabs = st.tabs(["Overview", "Training load", "Intensity", "Recovery",
                "Disciplines", "Strength"])

# =============================================================================
# 1. OVERVIEW
# =============================================================================
with tabs[0]:
    prog = metrics.progression_score(
        activities, daily, sleep, philosophy=philosophy,
        target_sessions=settings.weekly_session_target,
        strength=strength_sessions, through=TODAY, tags=session_tags)

    if prog is None:
        st.info("Not enough history yet to compute a progression score.")
    else:
        measured = sum(1 for v in prog["pillars"].values() if v is not None)
        delta = prog["delta"] or 0.0
        d_color = viz.STATUS["good"] if delta >= 0 else viz.STATUS["serious"]
        series = prog["series"].dropna(subset=["score"])
        span = (f'{charts._short_day(series["date"].iloc[0])} – '
                f'{charts._short_day(TODAY)}' if not series.empty else "")

        left, right = st.columns([1, 1.5], gap="small")
        with left:
            md(f"""<div class="card">
              <div class="kicker">Progression score</div>
              <div style="display:flex;align-items:baseline;gap:14px;margin-top:10px">
                <div class="display" style="font-size:96px;color:{prog['color']}">
                  {prog['score']:.0f}</div>
                <div style="display:flex;flex-direction:column;gap:8px">
                  {pill(prog['label'], prog['color'])}
                  <span style="font-size:13.5px;font-weight:700;color:{d_color}">
                    {'▲' if delta >= 0 else '▼'} {abs(delta):.1f}
                    <span style="color:{viz.MUTED};font-weight:500">/ 14 days</span>
                  </span>
                </div>
              </div>
              <div style="font-size:15px;color:{viz.INK_2};margin-top:18px;
                          line-height:1.45">{prog['verdict']}</div>
              <div class="muted" style="margin-top:14px;padding-top:14px;
                   border-top:1px solid {viz.HAIR}">
                {'Resting on all 5 pillars — every input is measured.'
                 if measured == 5 else
                 f'Resting on {measured} of 5 pillars — unmeasured pillars are '
                 'excluded, not counted against you.'}</div>
            </div>""")
        with right:
            md(f"""<div class="card">
              <div style="display:flex;justify-content:space-between;align-items:baseline">
                <div class="kicker">Score over time</div>
                <div class="muted">{span}</div>
              </div>
              <div style="margin-top:10px">
                {charts.progression_spark(prog['series'], prog['color'])}
              </div></div>""")

        st.write("")
        meta = [
            ("fitness", "Fitness", "Chronic load ramping"),
            ("efficiency", "Efficiency", "Pace per heartbeat, aerobic runs"),
            ("recovery", "Recovery", "HRV vs your normal · sleep · form"),
            ("consistency", "Consistency", "Sessions in, gaps out"),
            ("intensity", "Intensity", "Easy/hard split vs the models"),
        ]
        for col, (key, label, note) in zip(st.columns(5), meta, strict=False):
            v = prog["pillars"].get(key)
            null = v is None or not np.isfinite(v)
            c = viz.MUTED if null else viz.band_color(v)
            border = (viz.tint(viz.STATUS["serious"], .45)
                      if key == prog["worst"] and not null else viz.HAIR)
            tag = ("WEAKEST" if key == prog["worst"]
                   else "STRONGEST" if key == prog["best"] else "")
            tag_c = (viz.STATUS["serious"] if key == prog["worst"]
                     else viz.STATUS["good"])
            with col:
                md(f"""<div class="card-sm" style="border-color:{border}">
                  <div style="display:flex;justify-content:space-between;
                              align-items:center;gap:6px">
                    <span style="font-size:13px;font-weight:600;color:{viz.INK_2}">
                      {label}</span>
                    <span style="font-size:9.5px;font-weight:800;letter-spacing:.08em;
                                 color:{tag_c}">{tag if not null else ''}</span>
                  </div>
                  <div class="display" style="font-size:38px;color:{c};margin-top:12px">
                    {'—' if null else f'{v:.0f}'}</div>
                  <div class="track"><div class="fill"
                    style="width:{0 if null else max(v, 3):.0f}%;background:{c}"></div></div>
                  <div class="muted" style="margin-top:9px">
                    {'Not measured — no qualifying data' if null else note}</div>
                </div>""")

    proj = (metrics.race_projection(activities, settings.race_date, through=TODAY)
            if settings.race_date else None)
    if proj:
        st.write("")
        md('<div class="h-sec">Race day</div>'
           f'<div class="sub">If you keep averaging '
           f'{proj["typical_daily_load"]:.0f} load/day and taper from '
           f'{proj["taper_start"]:%d %b}.</div>')
        st.write("")
        cols = st.columns(4)
        tiles = [("Days out", f"{proj['days_out']}", "", viz.INK),
                 ("Fitness now", f"{proj['ctl_now']:.0f}", "", viz.INK),
                 ("Fitness at race", f"{proj['ctl_race']:.0f}",
                  f"{proj['ctl_race'] - proj['ctl_now']:+.0f}", viz.INK),
                 ("Form at race", f"{proj['tsb_race']:+.0f}", proj["verdict"],
                  viz.status_color(proj["status"]))]
        for col, (k, v, s, c) in zip(cols, tiles, strict=False):
            with col:
                md(f"""<div class="card-sm"><div class="kicker">{k}</div>
                  <div class="display" style="font-size:34px;margin-top:8px;
                       color:{c}">{v}</div>
                  <div class="muted" style="margin-top:6px">{s}&nbsp;</div></div>""")

# =============================================================================
# 2. TRAINING LOAD
# =============================================================================
with tabs[1]:
    md('<div class="h-sec">Training load</div>'
       '<div class="sub">Is the work building fitness faster than fatigue?</div>')
    st.write("")

    ff_all = metrics.fitness_fatigue(activities, through=TODAY)
    ff = ff_all[ff_all["date"] >= cutoff] if not ff_all.empty else ff_all
    rr = metrics.ramp_rate(ff_all)
    ms_all = metrics.monotony_strain(activities, through=TODAY)
    ms = ms_all[ms_all["date"] >= cutoff] if not ms_all.empty else ms_all

    main, side = st.columns([1, .38], gap="small")
    with main:
        legend = "".join(
            f'<span style="display:flex;align-items:center;gap:6px">'
            f'<span style="width:{"14px" if bar else "10px"};'
            f'height:{"3px" if bar else "10px"};border-radius:2px;'
            f'background:{c};display:inline-block"></span>{lbl}</span>'
            for lbl, c, bar in [("Fitness · CTL 42-day", viz.CTL, True),
                                ("Fatigue · ATL 7-day", viz.ATL, True),
                                ("Daily load", viz.LOAD_BAR, False)])
        md(f"""<div class="card">
          <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:12px;
                      color:{viz.MUTED_2};margin-bottom:8px">{legend}</div>
          {charts.load_chart(ff)}
          <div style="display:flex;justify-content:space-between;align-items:baseline;
                      margin:14px 0 6px">
            <div class="kicker">Form · TSB</div>
            <div class="muted"><span style="color:#96a672">above 0 fresh</span> ·
              <span style="color:#b3805f">below 0 fatigued</span></div>
          </div>
          {charts.tsb_chart(ff)}
        </div>""")
    with side:
        if rr:
            c = viz.status_color(rr["status"])
            md(f"""<div class="card-sm"><div class="kicker">Ramp rate</div>
              <div style="display:flex;align-items:baseline;gap:8px;margin-top:10px">
                <span class="display" style="font-size:40px;color:{c}">
                  {rr['per_week']:+.1f}</span>
                <span class="muted">CTL / week</span></div>
              <div style="margin-top:10px">{pill(rr['label'], c)}
                <span class="muted"> CTL {rr['ctl']:.0f} · 28-day window</span></div>
              <div class="muted" style="margin-top:12px">Sustained growth above
                ~5–7 / week is where injury and illness risk climbs.</div>
            </div>""")
        st.write("")
        mono_last = ms_all.dropna(subset=["monotony"])
        mono_v = f'{mono_last["monotony"].iloc[-1]:.2f}' if not mono_last.empty else "—"
        strain_v = f'{mono_last["strain"].iloc[-1]:.0f}' if not mono_last.empty else "—"
        md(f"""<div class="card-sm">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <div class="kicker">Monotony</div><div class="muted">7-day</div></div>
          <div style="display:flex;align-items:baseline;gap:10px;margin-top:10px">
            <span class="display" style="font-size:32px">{mono_v}</span>
            <span class="muted">strain {strain_v}</span></div>
          <div style="margin-top:10px">{charts.monotony_chart(ms)}</div>
          <div class="muted" style="margin-top:8px">Same-ish load every day pushes
            monotony up; varied days keep it low. Undefined below 3 training days.</div>
        </div>""")

    with st.expander("Table view"):
        t = ff[["date", "load", "ctl", "atl", "tsb"]].copy()
        num = t.select_dtypes("number").columns
        t[num] = t[num].round(1)
        st.dataframe(t.iloc[::-1], width="stretch", hide_index=True,
                     column_config={"date": st.column_config.DateColumn("Date"),
                                    "load": "Daily load", "ctl": "Fitness",
                                    "atl": "Fatigue", "tsb": "Form"})

# =============================================================================
# 3. INTENSITY  (+ the manual override)
# =============================================================================
with tabs[2]:
    md('<div class="h-sec">Intensity</div>'
       '<div class="sub">Are the easy days easy enough?</div>')
    st.write("")

    hq = metrics.hr_quality_summary(acts, tags=session_tags)
    tid = metrics.intensity_summary(acts, source=zone_source, tags=session_tags)

    if tid is None:
        st.info("No time-in-zone data in this window. Run "
                "`python track.py backfill`, then reload.")
    else:
        sh = tid["shares"]
        rows = [("You · this window", sh["low"], sh["moderate"], sh["high"], True)]
        rows += [(m.capitalize(), *v, False) for m, v in metrics.TID_MODELS.items()]

        legend = "".join(
            f'<span style="display:flex;align-items:center;gap:6px">'
            f'<span style="width:10px;height:10px;border-radius:3px;'
            f'background:{viz.BAND_COLORS[b]}"></span>{viz.BAND_LABEL[b]}</span>'
            for b in ("low", "moderate", "high"))

        bars = ""
        for name, lo, mo, hi, you in rows:
            caption = (f'{tid["hours"]:.1f} h measured' if you else "reference model")
            segs = "".join(
                f'<div style="width:{v*100:.1f}%;background:{viz.BAND_COLORS[b]};'
                f'color:{viz.BAND_TEXT[b]}">{f"{v*100:.0f}%" if v >= .09 else ""}</div>'
                for b, v in (("low", lo), ("moderate", mo), ("high", hi)))
            bars += (
                f'<div><div style="display:flex;justify-content:space-between;'
                f'font-size:12.5px;margin-bottom:6px">'
                f'<span style="font-weight:{800 if you else 500};'
                f'color:{viz.INK if you else viz.MUTED}">{name}</span>'
                f'<span style="color:{viz.MUTED}">{caption}</span></div>'
                f'<div class="stack" style="height:{30 if you else 20}px">{segs}</div>'
                f'</div>')

        main, side = st.columns([1, .38], gap="small")
        with main:
            md(f"""<div class="card">
              <div style="display:flex;gap:16px;font-size:12px;color:{viz.MUTED_2};
                          margin-bottom:14px">{legend}</div>
              <div style="display:flex;flex-direction:column;gap:14px">{bars}</div>
            </div>""")
        with side:
            c = viz.status_color(tid["status"])
            by_src = tid.get("sessions_by_source") or {}
            src_line = " · ".join(f"{v} {k}" for k, v in by_src.items())
            # Treff's Polarization Index — shown only when it is defined (needs
            # some time above threshold). ≥ 2.00 marks a genuinely polarised block.
            pi = tid.get("polarization_index")
            pi_row = "" if pi is None else (
                f'<div style="display:flex;justify-content:space-between">'
                f'<span>Polarization index</span>'
                f'<span style="color:{viz.INK};font-weight:600">'
                f'{pi:.2f} ({"polarised" if tid["polarized"] else "not polarised"})'
                f'</span></div>')
            md(f"""<div class="card-sm"><div class="kicker">This window</div>
              <div style="display:flex;align-items:baseline;gap:8px;margin-top:10px">
                <span class="display" style="font-size:40px;color:{c}">
                  {sh['low']*100:.0f}%</span>
                <span class="muted">time easy</span></div>
              <div style="margin-top:10px">{pill(tid['label'], c)}</div>
              <div style="display:flex;flex-direction:column;gap:8px;margin-top:16px;
                          font-size:13px;color:{viz.MUTED_2}">
                <div style="display:flex;justify-content:space-between"><span>Measured</span>
                  <span style="color:{viz.INK};font-weight:600">{tid['hours']:.1f} h</span></div>
                <div style="display:flex;justify-content:space-between"><span>Score</span>
                  <span style="color:{viz.INK};font-weight:600">{tid['score']:.0f} / 100</span></div>
                <div style="display:flex;justify-content:space-between"><span>Nearest model</span>
                  <span style="color:{viz.INK};font-weight:600">{tid['nearest_model']}</span></div>
                {pi_row}
              </div>
              <div class="muted" style="margin-top:14px">Scored from {src_line}.
                Both models that hold up put ~75–80% of time easy.</div>
            </div>""")

    # --- Manual override ----------------------------------------------------
    st.write("")
    with st.container(border=True):
        md('<div class="kicker">Correct a session</div>')
        unresolved = hq["unresolved"] if hq else 0
        md(f'<div class="muted" style="margin:6px 0 12px">'
           f'A wrist sensor tracks foot-strike cadence while you run and reports '
           f'it as heart rate. {"<b>" + str(unresolved) + " runs</b> here still look hard on the sensor and haven&rsquo;t been checked. " if unresolved else ""}'
           f'You were there and it was inferring — tag a session and your read of '
           f'it wins outright, no chest strap required.</div>')

        tagged = metrics.apply_tags(acts, session_tags)
        q = metrics.hr_quality(tagged)
        pick = q[q["sport"].isin(["run", "bike", "swim"])].sort_values(
            "date", ascending=False)

        only_flagged = st.checkbox(
            "Only show sessions the sensor called hard", value=True,
            help="Runs where heart rate disagrees with power, or claims most of "
                 "the session was above threshold.")
        if only_flagged:
            pick = pick[pick["hr_suspect"] | (pick["hr_hard_share"] > 0.5)]

        if pick.empty:
            st.caption("Nothing to correct in this window.")
        else:
            def _label(r) -> str:
                mins = (r["duration_s"] or 0) / 60
                mark = f"  ·  tagged {r['feel']}" if pd.notna(r.get("feel")) else ""
                hard = (f"  ·  sensor says {r['hr_hard_share']*100:.0f}% hard"
                        if pd.notna(r["hr_hard_share"]) else "")
                return (f"{r['date']:%a %d %b}  ·  {r['sport']}  ·  "
                        f"{mins:.0f} min{hard}{mark}")

            options = list(pick["activity_id"])
            labels = {int(r["activity_id"]): _label(r) for _, r in pick.iterrows()}
            c1, c2, c3 = st.columns([2.4, 1, 1])
            chosen = c1.selectbox("Session", options,
                                  format_func=lambda a: labels[int(a)])
            cur = pick[pick["activity_id"] == chosen].iloc[0]
            cur_feel = cur["feel"] if pd.notna(cur.get("feel")) else None
            feel = c2.radio("How did it actually feel?", metrics.FEEL_ORDER,
                            index=metrics.FEEL_ORDER.index(cur_feel) if cur_feel else 0,
                            help="Easy = you could hold a conversation and breathe "
                                 "through your nose. That's the talk test, a "
                                 "validated stand-in for your first threshold.")
            rpe = c3.number_input("RPE (optional)", 1.0, 10.0,
                                  float(cur["rpe"]) if pd.notna(cur.get("rpe")) else 3.0,
                                  step=1.0)

            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("Save tag", width="stretch"):
                with db.connect() as conn:
                    db.upsert_session_tag(conn, {
                        "activity_id": int(chosen),
                        "date": pd.Timestamp(cur["date"]).strftime("%Y-%m-%d"),
                        "rpe": float(rpe), "feel": feel, "note": None,
                        "tagged_at": datetime.now().isoformat(timespec="seconds")})
                st.cache_data.clear()
                st.rerun()
            if cur_feel and b2.button("Clear tag", width="stretch"):
                with db.connect() as conn:
                    conn.execute("DELETE FROM session_tags WHERE activity_id = ?",
                                 (int(chosen),))
                st.cache_data.clear()
                st.rerun()

        cov = metrics.tag_coverage(acts, session_tags)
        st.caption(f"{cov['tagged']} of {cov['total']} sessions in this window "
                   f"carry your own read. Tags only apply on the Auto source.")

        cmp = metrics.feel_vs_zones(acts, session_tags)
        if not cmp.empty:
            disagree = int((~cmp["agrees"].fillna(True)).sum())
            with st.expander(f"Where you and the sensor disagree · {disagree} of {len(cmp)}"):
                t = cmp.copy()
                t["date"] = pd.to_datetime(t["date"]).dt.date
                st.dataframe(
                    t[["date", "sport", "feel", "rpe", "sensor_band", "agrees",
                       "low_pct", "high_pct", "zone_source"]].round(1),
                    width="stretch", hide_index=True,
                    column_config={"feel": "You said", "sensor_band": "Sensor said",
                                   "low_pct": "Easy %", "high_pct": "Hard %",
                                   "zone_source": "Scored from"})

    # --- Per discipline + weekly --------------------------------------------
    by_sport = metrics.intensity_distribution(acts, by_sport=True,
                                              source=zone_source, tags=session_tags)
    if not by_sport.empty:
        st.write("")
        tot = by_sport.groupby("sport")[["low_s", "moderate_s", "high_s"]].sum()
        tot = tot.div(tot.sum(axis=1), axis=0) * 100
        body = ""
        for sport in viz.SPORT_ORDER:
            if sport not in tot.index:
                continue
            r = tot.loc[sport]
            segs = "".join(
                f'<div style="width:{r[f"{b}_s"]:.1f}%;'
                f'background:{viz.BAND_COLORS[b]}"></div>'
                for b in ("low", "moderate", "high"))
            body += (
                f'<div style="display:grid;grid-template-columns:70px 1fr 120px;'
                f'gap:12px;align-items:center">'
                f'<span style="display:flex;align-items:center;gap:7px;font-size:13px;'
                f'font-weight:600"><span style="width:9px;height:9px;border-radius:99px;'
                f'background:{viz.SPORT_COLORS[sport]}"></span>'
                f'{viz.SPORT_NAMES[sport]}</span>'
                f'<div class="stack" style="height:14px">{segs}</div>'
                f'<span style="font-size:12px;color:{viz.MUTED};text-align:right">'
                f'{r["low_s"]:.0f} / {r["moderate_s"]:.0f} / {r["high_s"]:.0f}</span></div>')
        wk = metrics.intensity_distribution(acts, source=zone_source, tags=session_tags)
        md(f"""<div class="card">
          <div class="kicker" style="margin-bottom:12px">By discipline</div>
          <div style="display:flex;flex-direction:column;gap:12px">{body}</div>
          <div style="height:1px;background:{viz.HAIR};margin:20px 0"></div>
          <div class="kicker" style="margin-bottom:8px">Week by week</div>
          {charts.weekly_intensity(wk)}
        </div>""")

# =============================================================================
# 4. RECOVERY
# =============================================================================
with tabs[3]:
    md('<div class="h-sec">Recovery</div>'
       '<div class="sub">Is the body absorbing the work?</div>')
    st.write("")

    hb_all = metrics.hrv_baseline(daily)
    hb = hb_all[hb_all["date"] >= cutoff] if not hb_all.empty else hb_all
    cur = hb_all.dropna(subset=["z"])
    hrv_map = {"normal": ("In range", viz.STATUS["good"]),
               "suppressed": ("Suppressed", viz.STATUS["serious"]),
               "elevated": ("Elevated", "#93b356")}
    if not cur.empty:
        lbl, hc = hrv_map.get(cur.iloc[-1]["status"], ("—", viz.MUTED))
        hrv_pill = pill(f'{lbl} · z {cur.iloc[-1]["z"]:+.2f}', hc)
    else:
        hrv_pill = pill("No baseline yet", viz.MUTED)

    rhr = metrics.rolling_metric(day, "resting_hr")
    eff_all = metrics.run_efficiency(acts)
    eff_n = int(eff_all["aerobic"].sum()) if not eff_all.empty else 0
    slp_valid = slp.dropna(subset=["total_sleep_s"]) if not slp.empty else slp
    if not slp_valid.empty:
        avg = slp_valid["total_sleep_s"].mean()
        sleep_avg = f"{int(avg // 3600)}h {int(avg % 3600 // 60):02d}m"
    else:
        sleep_avg = "—"

    a, b = st.columns(2, gap="small")
    with a:
        md(f"""<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;
                      gap:8px"><div style="font-size:13px;font-weight:700">
            Overnight HRV <span style="color:{viz.MUTED};font-weight:500">
            · 7-day rolling</span></div>{hrv_pill}</div>
          <div style="margin-top:12px">{charts.hrv_chart(hb)}</div>
          <div class="muted" style="margin-top:8px">Shaded band = your normal range
            (±0.5 SD of a 60-day baseline). The nightly number alone is mostly noise.</div>
        </div>""")
    with b:
        now = f'{rhr["resting_hr"].iloc[-1]:.0f}' if not rhr.empty else "—"
        roll = f'{rhr["rolling"].iloc[-1]:.1f}' if not rhr.empty else "—"
        md(f"""<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:13px;font-weight:700">Resting heart rate
              <span style="color:{viz.MUTED};font-weight:500">· lower is better</span></div>
            <span style="font-size:12.5px;color:{viz.MUTED_2}">now
              <b style="color:{viz.INK}">{now}</b> · 7-day {roll}</span></div>
          <div style="margin-top:12px">{charts.rhr_chart(rhr)}</div>
        </div>""")

    st.write("")
    c, d = st.columns(2, gap="small")
    with c:
        md(f"""<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:13px;font-weight:700">Sleep
              <span style="color:{viz.MUTED};font-weight:500">· vs 8 h target</span></div>
            <span style="font-size:12.5px;color:{viz.MUTED_2}">avg
              <b style="color:{viz.INK}">{sleep_avg}</b></span></div>
          <div style="margin-top:12px">{charts.sleep_chart(slp)}</div>
          <div class="muted" style="margin-top:8px">Gaps are nights the watch
            recorded nothing.</div></div>""")
    with d:
        # run_efficiency() has already dropped runs whose HR failed the quality
        # check — distance per heartbeat is meaningless if the heartbeat was
        # cadence — so say so rather than leaving a mysteriously empty chart.
        n_runs = int((acts["sport"] == "run").sum()) if not acts.empty else 0
        excluded = max(0, n_runs - len(eff_all))
        tail = (f" {excluded} more were left out because their heart-rate trace "
                "didn't hold up." if excluded else "")
        if eff_all.empty:
            note = ("No run in this window has a heart-rate trace worth "
                    f"trending.{tail}")
        elif eff_n == 0:
            note = (f"None of the {len(eff_all)} usable runs were steady and "
                    "aerobic enough to compare like for like. Faded dots are the "
                    f"non-qualifying runs, for context.{tail}")
        elif eff_n < 3:
            note = (f"Only {eff_n} of {len(eff_all)} usable runs qualify as steady "
                    f"aerobic — too few to call a trend.{tail}")
        else:
            note = f"Higher is better — more distance held per heartbeat.{tail}"
        md(f"""<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:13px;font-weight:700">Run efficiency
              <span style="color:{viz.MUTED};font-weight:500">· steady aerobic only</span></div>
            {pill(f'{eff_n} qualifying', viz.STATUS['warning'] if eff_n < 3 else viz.STATUS['good'])}
          </div>
          <div style="margin-top:12px">{charts.efficiency_chart(eff_all)}</div>
          <div class="muted" style="margin-top:8px">{note}</div></div>""")

    vo2 = day["vo2max_run"].dropna() if "vo2max_run" in day else pd.Series(dtype=float)
    vo2b = day["vo2max_bike"].dropna() if "vo2max_bike" in day else pd.Series(dtype=float)
    if not vo2.empty or not vo2b.empty:
        parts = []
        if not vo2.empty:
            parts.append(f"running **{vo2.iloc[-1]:.1f}**")
        if not vo2b.empty:
            parts.append(f"cycling **{vo2b.iloc[-1]:.1f}**")
        st.caption("Latest VO₂max estimate: " + " · ".join(parts) + " — wearable "
                   "estimates move far more slowly than real fitness, and the "
                   "run and bike engines are tracked separately.")

# =============================================================================
# 5. DISCIPLINES
# =============================================================================
with tabs[4]:
    md('<div class="h-sec">Disciplines</div>'
       '<div class="sub">Is the swim / bike / run mix right for race day?</div>')
    st.write("")

    bal = metrics.discipline_balance(activities, through=TODAY)
    rows = ""
    for _, r in bal.iterrows():
        c = viz.SPORT_COLORS[r["sport"]]
        share = 0 if not np.isfinite(r["share"]) else r["share"] * 100
        gap = r["gap_pct"] if np.isfinite(r["gap_pct"]) else 0
        gap_c = viz.STATUS["warning"] if abs(gap) > 10 else viz.MUTED
        ds = r["days_since"]
        last = ("never recorded" if ds is None else
                f"stale · {ds} d" if r["stale"] else
                "today" if ds == 0 else f"{ds} d ago")
        rows += (
            f'<div style="display:grid;grid-template-columns:64px 1fr 230px;'
            f'gap:14px;align-items:center">'
            f'<span style="display:flex;align-items:center;gap:8px;font-size:13.5px;'
            f'font-weight:700"><span style="width:10px;height:10px;border-radius:99px;'
            f'background:{c}"></span>{viz.SPORT_NAMES[r["sport"]]}</span>'
            f'<div style="position:relative;height:16px;border-radius:99px;'
            f'background:{viz.GRID_2}">'
            f'<div style="position:absolute;inset:0 auto 0 0;width:{share:.1f}%;'
            f'background:{c};border-radius:99px;opacity:.9"></div>'
            f'<div style="position:absolute;top:-4px;bottom:-4px;'
            f'left:{r["target_share"]*100:.0f}%;width:2.5px;background:{viz.INK};'
            f'border-radius:2px"></div></div>'
            f'<div style="display:flex;align-items:center;justify-content:flex-end;'
            f'gap:8px;font-size:12px">'
            f'<span style="color:{viz.INK};font-weight:700">{share:.0f}%</span>'
            f'<span style="color:{gap_c};font-weight:600">{gap:+.1f} pt</span>'
            f'<span style="color:{viz.MUTED};background:{viz.GRID_2};padding:2px 9px;'
            f'border-radius:99px;white-space:nowrap">{last}</span></div></div>')

    vol_legend = "".join(
        f'<span style="display:flex;align-items:center;gap:6px">'
        f'<span style="width:10px;height:10px;border-radius:3px;'
        f'background:{viz.SPORT_COLORS[s]}"></span>{viz.SPORT_NAMES[s]}</span>'
        for s in viz.SPORT_ORDER)
    md(f"""<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:baseline;
                  margin-bottom:14px;gap:12px;flex-wrap:wrap">
        <div class="kicker">Share of training time · last 28 days</div>
        <div class="muted">target 20 / 50 / 30 · tick = target</div></div>
      <div style="display:flex;flex-direction:column;gap:16px">{rows}</div>
      <div style="height:1px;background:{viz.HAIR};margin:20px 0"></div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;
                  margin-bottom:8px;gap:12px;flex-wrap:wrap">
        <div class="kicker">Weekly hours by sport</div>
        <div style="display:flex;gap:14px;font-size:12px;color:{viz.MUTED_2}">
          {vol_legend}</div></div>
      {charts.volume_chart(metrics.weekly_volume(acts))}
    </div>""")

# =============================================================================
# 6. STRENGTH
# =============================================================================
with tabs[5]:
    md('<div class="h-sec">Strength</div>'
       '<div class="sub">Gaining, holding, or giving it back?</div>')
    st.write("")
    if strength_sets.empty:
        st.info("No strength data yet. Export from Que and run "
                "`python track.py sync-que`.")
    else:
        trend = metrics.strength_trend(strength_sets)
        if trend is not None:
            status = {"Gaining": "good", "Maintaining": "warning",
                      "Losing": "critical"}[trend["verdict"]]
            c = viz.status_color(status)
            arrow = {"Gaining": "▲", "Maintaining": "▬", "Losing": "▼"}[trend["verdict"]]
            md(f"""<div class="card-sm" style="max-width:420px">
              <div class="kicker">Overall strength</div>
              <div class="display" style="font-size:36px;color:{c};margin-top:10px">
                {arrow} {trend['verdict']}</div>
              <div class="muted" style="margin-top:8px">
                {trend['overall_pct']:+.1f}% estimated 1-rep-max across
                {trend['n_lifts']} lifts</div></div>""")
            st.write("")
            per = pd.DataFrame(trend["per_exercise"]).rename(columns={
                "exercise": "Exercise", "trend": "Trend", "delta_pct": "Change %",
                "latest_e1rm": "Latest e1RM (lb)", "baseline_e1rm": "Baseline (lb)",
                "sessions": "Sessions"})
            st.dataframe(per[["Exercise", "Trend", "Change %", "Baseline (lb)",
                              "Latest e1RM (lb)", "Sessions"]],
                         width="stretch", hide_index=True)

meta = (f"{len(activities)} activities · {len(daily)} wellness days · "
        f"{charts._short_day(activities['date'].min())} – "
        f"{charts._short_day(last_any)} · generated {TODAY:%d %b %Y}"
        if not activities.empty else "")
md(f"""<div style="display:flex;justify-content:space-between;gap:16px;
     flex-wrap:wrap;font-size:12px;color:{viz.MUTED};margin-top:40px;
     padding-top:18px;border-top:1px solid {viz.HAIR}">
  <span>{meta}</span><span>Progression · a personal training tracker</span></div>""")
