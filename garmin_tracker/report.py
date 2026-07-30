"""Server-rendered HTML dashboard.

The same numbers and the same SVG charts as the Streamlit app, emitted as one
self-contained page. This is what gets deployed — a serverless function can
return HTML, but it cannot host Streamlit's long-lived websocket session.

Everything visual comes from ``viz`` (tokens) and ``charts`` (SVG), so the two
front-ends can't drift apart on colour or geometry.
"""

from __future__ import annotations

import html
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import charts, db, metrics, viz
from .config import settings


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def pill(text: str, color: str) -> str:
    return (f'<span class="pill" style="color:{color};'
            f'background:{viz.tint(color)}">{esc(text)}</span>')


def card(body: str, *, small: bool = False, style: str = "") -> str:
    cls = "card-sm" if small else "card"
    return f'<div class="{cls}" style="{style}">{body}</div>'


def kicker(text: str) -> str:
    return f'<div class="kicker">{esc(text)}</div>'


def tile(label: str, value: str, sub: str = "", color: str = viz.INK) -> str:
    return card(f'{kicker(label)}'
                f'<div class="display" style="font-size:34px;margin-top:8px;'
                f'color:{color}">{value}</div>'
                f'<div class="muted" style="margin-top:6px">{sub}&nbsp;</div>',
                small=True)


# ---------------------------------------------------------------------------

_CSS = f"""
*,*::before,*::after{{box-sizing:border-box}}
body{{margin:0;background:{viz.BG};color:{viz.INK};
     font-family:{viz.BODY_FONT};-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1240px;margin:0 auto;padding:0 24px 80px}}
a{{color:{viz.ACCENT};text-decoration:none}}
.card{{background:{viz.CARD};border:1px solid {viz.HAIR};border-radius:24px;padding:24px}}
.card-sm{{background:{viz.CARD};border:1px solid {viz.HAIR};border-radius:20px;padding:18px}}
.kicker{{font-size:11.5px;font-weight:700;letter-spacing:.12em;
        text-transform:uppercase;color:{viz.MUTED}}}
.display{{font-family:{viz.DISPLAY_FONT};line-height:.9}}
.h-sec{{font-family:{viz.DISPLAY_FONT};font-size:26px;margin:0}}
.sub{{font-size:14px;color:{viz.MUTED};margin-top:4px}}
.muted{{color:{viz.MUTED};font-size:12px;line-height:1.45}}
.pill{{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
      padding:3px 11px;border-radius:999px;display:inline-block;white-space:nowrap}}
.flag{{display:flex;align-items:flex-start;gap:14px;background:{viz.CARD};
      border:1px solid {viz.HAIR};border-radius:18px;padding:13px 18px;margin-bottom:9px}}
.flag-dot{{width:34px;height:34px;flex:none;border-radius:999px;display:flex;
          align-items:center;justify-content:center;font-weight:800}}
.track{{height:6px;border-radius:99px;background:{viz.GRID_2};margin-top:12px;overflow:hidden}}
.fill{{height:6px;border-radius:99px}}
.stack{{display:flex;border-radius:99px;overflow:hidden}}
.stack>div{{display:flex;align-items:center;justify-content:center;
           font-size:11px;font-weight:700}}
.grid{{display:grid;gap:12px}}
.nav{{position:sticky;top:0;z-index:40;background:rgba(33,28,23,.93);
     backdrop-filter:blur(8px);margin:0 -24px;padding:10px 24px;
     border-bottom:1px solid {viz.HAIR};display:flex;gap:6px;flex-wrap:wrap;
     font-size:13px;font-weight:600;overflow-x:auto}}
.nav a{{padding:7px 14px;border-radius:999px;color:{viz.MUTED_2};white-space:nowrap}}
.nav a:hover{{background:rgba(240,230,210,.06);color:{viz.INK}}}
section{{scroll-margin-top:70px;margin-top:44px}}
button{{font-family:inherit}}
.btn{{background:{viz.ACCENT};color:{viz.BG};border:none;border-radius:999px;
     font-weight:700;font-size:13px;padding:8px 16px;cursor:pointer}}
.btn:hover{{background:{viz.ACCENT_2}}}
.btn:disabled{{opacity:.55;cursor:progress}}
.g2{{grid-template-columns:1fr 1fr}}
.g4{{grid-template-columns:repeat(4,1fr)}}
.g5{{grid-template-columns:repeat(5,1fr)}}
.hero{{grid-template-columns:400px 1fr}}
.side{{grid-template-columns:1fr 300px}}

/* label · bar · figures. The fixed outer columns keep every bar starting and
   ending on the same line down the list, which is what makes the disciplines
   comparable at a glance. Below 640px there isn't width for three columns, so
   the bar drops to its own full-width row rather than being crushed to nothing.
   These live here rather than inline because an inline grid-template-columns
   outranks a media query and silently defeats it. */
.row3{{display:grid;align-items:center}}
.row-mix{{grid-template-columns:64px 1fr 220px;gap:14px}}
.row-band{{grid-template-columns:70px 1fr 110px;gap:12px}}

@media (max-width:1000px){{
  .g2,.g5,.hero,.side{{grid-template-columns:1fr}}
  .wrap{{padding:0 14px 60px}}
  .nav{{margin:0 -14px;padding:10px 14px}}
}}
@media (max-width:640px){{
  .g4{{grid-template-columns:repeat(2,1fr)}}
  .row-mix,.row-band{{grid-template-columns:1fr auto;gap:8px 10px}}
  .row-mix>:nth-child(1),.row-band>:nth-child(1){{grid-area:1/1}}
  .row-mix>:nth-child(3),.row-band>:nth-child(3){{grid-area:1/2}}
  .row-mix>:nth-child(2),.row-band>:nth-child(2){{grid-area:2/1/3/3}}
}}
"""

# Inline so the page stays self-contained and the browser stops asking for
# /favicon.ico (a 404 on every load otherwise).
_FAVICON = (
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E"
    f"%3Crect width='24' height='24' rx='6' fill='{viz.ACCENT.replace('#', '%23')}'/%3E"
    "%3Cpath d='M22 12h-4l-3 9L9 3l-3 9H2' fill='none' "
    f"stroke='{viz.BG.replace('#', '%23')}' stroke-width='2.5' "
    "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")

_JS = """
const btn = document.getElementById('syncBtn');
const out = document.getElementById('syncMsg');
if (btn) btn.addEventListener('click', async () => {
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Syncing…';
  out.textContent = 'Contacting Garmin — this can take up to a minute.';
  try {
    const r = await fetch('/api/sync', { method: 'POST' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    out.textContent = `Synced ${j.activities} activities · ${j.wellness_days} wellness days. Reloading…`;
    setTimeout(() => location.reload(), 900);
  } catch (e) {
    out.textContent = 'Sync failed: ' + e.message;
    btn.disabled = false;
    btn.textContent = original;
  }
});
"""


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _header(stale: int | None, race: dict | None, show_sync: bool) -> str:
    sync_color = viz.STATUS["good"] if stale == 0 else viz.STATUS["warning"]
    sync_label = ("Synced today" if stale == 0 else
                  f"{stale} days stale" if stale is not None else "No data")
    race_html = ""
    if race:
        race_html = (
            f'<div style="display:flex;align-items:center;gap:8px;'
            f'background:{viz.tint(viz.ACCENT)};border:1px solid '
            f'{viz.tint(viz.ACCENT, .35)};border-radius:999px;padding:7px 14px;'
            f'font-size:12.5px;color:{viz.ACCENT_2};font-weight:600">'
            f'⏱ {race["days"]} days to {esc(race["name"])}</div>')
    sync_btn = ""
    if show_sync:
        sync_btn = ('<button class="btn" id="syncBtn">⟳ Sync now</button>'
                    '<span id="syncMsg" class="muted"></span>')
    return f"""
<div style="display:flex;align-items:center;justify-content:space-between;
     gap:16px;padding:26px 0 10px;flex-wrap:wrap">
  <div style="display:flex;align-items:center;gap:12px">
    <div style="width:34px;height:34px;border-radius:999px;background:{viz.ACCENT};
         display:flex;align-items:center;justify-content:center">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{viz.BG}"
        stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
    <div><div class="display" style="font-size:20px">Progression</div>
      <div style="font-size:12px;color:{viz.MUTED};margin-top:2px">
        Ironman training · Garmin data</div></div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    {sync_btn}
    <div style="display:flex;align-items:center;gap:7px;background:{viz.CARD};
         border:1px solid {viz.HAIR};border-radius:999px;padding:7px 14px;
         font-size:12.5px;color:{viz.MUTED_2}">
      <span style="width:7px;height:7px;border-radius:99px;background:{sync_color}"></span>
      {sync_label}</div>{race_html}
  </div>
</div>
<div class="nav">
  <a href="#overview">Overview</a><a href="#load">Training load</a>
  <a href="#intensity">Intensity</a><a href="#recovery">Recovery</a>
  <a href="#disciplines">Disciplines</a>
</div>"""


def _flags(flags: list[dict]) -> str:
    out = []
    for f in flags[:5]:
        c = viz.status_color(f["status"])
        out.append(
            f'<div class="flag"><div class="flag-dot" '
            f'style="background:{viz.tint(c)};color:{c}">!</div>'
            f'<div style="flex:1;min-width:0">'
            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<span style="font-weight:700;font-size:14px">{esc(f["title"])}</span>'
            f'{pill(f["status"], c)}</div>'
            f'<div style="font-size:13px;color:{viz.MUTED_2};margin-top:3px">'
            f'{esc(f["detail"])}</div></div></div>')
    return ('<div style="margin-top:18px">' + "".join(out) + "</div>") if out else ""


def _overview(prog: dict | None, proj: dict | None) -> str:
    if prog is None:
        return ('<section id="overview">' +
                card('<div class="muted">Not enough history yet to compute a '
                     'progression score.</div>') + "</section>")
    measured = sum(1 for v in prog["pillars"].values() if v is not None)
    delta = prog["delta"] or 0.0
    d_color = viz.STATUS["good"] if delta >= 0 else viz.STATUS["serious"]
    series = prog["series"].dropna(subset=["score"])
    span = (f'{charts._short_day(series["date"].iloc[0])} – '
            f'{charts._short_day(series["date"].iloc[-1])}' if not series.empty else "")

    hero = card(
        f'{kicker("Progression score")}'
        f'<div style="display:flex;align-items:baseline;gap:14px;margin-top:10px">'
        f'<div class="display" style="font-size:96px;color:{prog["color"]}">'
        f'{prog["score"]:.0f}</div>'
        f'<div style="display:flex;flex-direction:column;gap:8px">'
        f'{pill(prog["label"], prog["color"])}'
        f'<span style="font-size:13.5px;font-weight:700;color:{d_color}">'
        f'{"▲" if delta >= 0 else "▼"} {abs(delta):.1f}'
        f'<span style="color:{viz.MUTED};font-weight:500"> / 14 days</span></span>'
        f'</div></div>'
        f'<div style="font-size:15px;color:{viz.INK_2};margin-top:18px;'
        f'line-height:1.45">{esc(prog["verdict"])}</div>'
        f'<div class="muted" style="margin-top:14px;padding-top:14px;'
        f'border-top:1px solid {viz.HAIR}">'
        + ("Resting on all 5 pillars — every input is measured."
           if measured == 5 else
           f"Resting on {measured} of 5 pillars — unmeasured pillars are "
           "excluded, not counted against you.") + "</div>")

    spark = card(
        f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
        f'{kicker("Score over time")}<div class="muted">{span}</div></div>'
        f'<div style="margin-top:10px">'
        f'{charts.progression_spark(prog["series"], prog["color"])}</div>')

    meta = [("fitness", "Fitness", "Chronic load ramping"),
            ("efficiency", "Efficiency", "Pace per heartbeat, aerobic runs"),
            ("recovery", "Recovery", "HRV vs your normal · sleep · form"),
            ("consistency", "Consistency", "Sessions in, gaps out"),
            ("intensity", "Intensity", "Easy/hard split vs the models")]
    cards = []
    for key, label, note in meta:
        v = prog["pillars"].get(key)
        null = v is None or not np.isfinite(v)
        c = viz.MUTED if null else viz.band_color(v)
        border = (viz.tint(viz.STATUS["serious"], .45)
                  if key == prog["worst"] and not null else viz.HAIR)
        tag = ("WEAKEST" if key == prog["worst"] and not null
               else "STRONGEST" if key == prog["best"] and not null else "")
        tag_c = viz.STATUS["serious"] if key == prog["worst"] else viz.STATUS["good"]
        cards.append(card(
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:center;gap:6px">'
            f'<span style="font-size:13px;font-weight:600;color:{viz.INK_2}">{label}</span>'
            f'<span style="font-size:9.5px;font-weight:800;letter-spacing:.08em;'
            f'color:{tag_c}">{tag}</span></div>'
            f'<div class="display" style="font-size:38px;color:{c};margin-top:12px">'
            f'{"—" if null else f"{v:.0f}"}</div>'
            f'<div class="track"><div class="fill" style="width:'
            f'{0 if null else max(v, 3):.0f}%;background:{c}"></div></div>'
            f'<div class="muted" style="margin-top:9px">'
            f'{"Not measured — no qualifying data" if null else note}</div>',
            small=True, style=f"border-color:{border}"))

    race = ""
    if proj:
        tiles = [tile("Days out", f'{proj["days_out"]}'),
                 tile("Fitness now", f'{proj["ctl_now"]:.0f}'),
                 tile("Fitness at race", f'{proj["ctl_race"]:.0f}',
                      f'{proj["ctl_race"] - proj["ctl_now"]:+.0f}'),
                 tile("Form at race", f'{proj["tsb_race"]:+.0f}', proj["verdict"],
                      viz.status_color(proj["status"]))]
        race = (f'<div style="margin-top:34px"><div class="h-sec">Race day</div>'
                f'<div class="sub">If you keep averaging '
                f'{proj["typical_daily_load"]:.0f} load/day and taper from '
                f'{proj["taper_start"]:%d %b}.</div>'
                f'<div class="grid g4" style="margin-top:14px">'
                f'{"".join(tiles)}</div></div>')

    return (f'<section id="overview" style="margin-top:14px">'
            f'<div class="grid hero">{hero}{spark}</div>'
            f'<div class="grid g5" style="margin-top:12px">{"".join(cards)}</div>'
            f'{race}</section>')


def _load(ff: pd.DataFrame, rr: dict | None, ms: pd.DataFrame) -> str:
    legend = "".join(
        f'<span style="display:flex;align-items:center;gap:6px">'
        f'<span style="width:{"14px" if bar else "10px"};'
        f'height:{"3px" if bar else "10px"};border-radius:2px;background:{c}"></span>'
        f'{lbl}</span>'
        for lbl, c, bar in [("Fitness · CTL 42-day", viz.CTL, True),
                            ("Fatigue · ATL 7-day", viz.ATL, True),
                            ("Daily load", viz.LOAD_BAR, False)])
    main = card(
        f'<div style="display:flex;gap:18px;flex-wrap:wrap;font-size:12px;'
        f'color:{viz.MUTED_2};margin-bottom:8px">{legend}</div>'
        f'{charts.load_chart(ff)}'
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'margin:14px 0 6px">{kicker("Form · TSB")}'
        f'<div class="muted"><span style="color:#96a672">above 0 fresh</span> · '
        f'<span style="color:#b3805f">below 0 fatigued</span></div></div>'
        f'{charts.tsb_chart(ff)}')

    side = ""
    if rr:
        c = viz.status_color(rr["status"])
        side += card(
            f'{kicker("Ramp rate")}'
            f'<div style="display:flex;align-items:baseline;gap:8px;margin-top:10px">'
            f'<span class="display" style="font-size:40px;color:{c}">'
            f'{rr["per_week"]:+.1f}</span><span class="muted">CTL / week</span></div>'
            f'<div style="margin-top:10px">{pill(rr["label"], c)}'
            f'<span class="muted"> CTL {rr["ctl"]:.0f} · 28-day window</span></div>'
            f'<div class="muted" style="margin-top:12px">Sustained growth above '
            f'~5–7 / week is where injury and illness risk climbs.</div>', small=True)
    valid = ms.dropna(subset=["monotony"]) if not ms.empty else ms
    mono = f'{valid["monotony"].iloc[-1]:.2f}' if not valid.empty else "—"
    strain = f'{valid["strain"].iloc[-1]:.0f}' if not valid.empty else "—"
    side += ('<div style="margin-top:14px"></div>' + card(
        f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
        f'{kicker("Monotony")}<div class="muted">7-day</div></div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;margin-top:10px">'
        f'<span class="display" style="font-size:32px">{mono}</span>'
        f'<span class="muted">strain {strain}</span></div>'
        f'<div style="margin-top:10px">{charts.monotony_chart(ms)}</div>'
        f'<div class="muted" style="margin-top:8px">Same-ish load every day pushes '
        f'monotony up; varied days keep it low. Undefined below 3 training days.</div>',
        small=True))

    return (f'<section id="load"><div class="h-sec">Training load</div>'
            f'<div class="sub">Is the work building fitness faster than fatigue?</div>'
            f'<div class="grid side" style="margin-top:16px">{main}'
            f'<div>{side}</div></div></section>')


def _intensity(tid: dict | None, by_sport: pd.DataFrame, wk: pd.DataFrame,
               hq: dict | None) -> str:
    if tid is None:
        return ('<section id="intensity"><div class="h-sec">Intensity</div>'
                + card('<div class="muted">No time-in-zone data yet.</div>')
                + "</section>")
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
        caption = f'{tid["hours"]:.1f} h measured' if you else "reference model"
        segs = "".join(
            f'<div style="width:{v*100:.1f}%;background:{viz.BAND_COLORS[b]};'
            f'color:{viz.BAND_TEXT[b]}">{f"{v*100:.0f}%" if v >= .09 else ""}</div>'
            for b, v in (("low", lo), ("moderate", mo), ("high", hi)))
        bars += (f'<div><div style="display:flex;justify-content:space-between;'
                 f'font-size:12.5px;margin-bottom:6px">'
                 f'<span style="font-weight:{800 if you else 500};'
                 f'color:{viz.INK if you else viz.MUTED}">{name}</span>'
                 f'<span style="color:{viz.MUTED}">{caption}</span></div>'
                 f'<div class="stack" style="height:{30 if you else 20}px">{segs}</div></div>')

    sport_rows = ""
    if not by_sport.empty:
        tot = by_sport.groupby("sport")[["low_s", "moderate_s", "high_s"]].sum()
        tot = tot.div(tot.sum(axis=1), axis=0) * 100
        for sport in viz.SPORT_ORDER:
            if sport not in tot.index:
                continue
            r = tot.loc[sport]
            segs = "".join(f'<div style="width:{r[f"{b}_s"]:.1f}%;'
                           f'background:{viz.BAND_COLORS[b]}"></div>'
                           for b in ("low", "moderate", "high"))
            sport_rows += (
                f'<div class="row3 row-band">'
                f'<span style="display:flex;align-items:center;gap:7px;font-size:13px;'
                f'font-weight:600"><span style="width:9px;height:9px;border-radius:99px;'
                f'background:{viz.SPORT_COLORS[sport]}"></span>'
                f'{viz.SPORT_NAMES[sport]}</span>'
                f'<div class="stack" style="height:14px">{segs}</div>'
                f'<span style="font-size:12px;color:{viz.MUTED};text-align:right">'
                f'{r["low_s"]:.0f} / {r["moderate_s"]:.0f} / {r["high_s"]:.0f}</span></div>')

    main = card(
        f'<div style="display:flex;gap:16px;font-size:12px;color:{viz.MUTED_2};'
        f'margin-bottom:14px">{legend}</div>'
        f'<div style="display:flex;flex-direction:column;gap:14px">{bars}</div>'
        + (f'<div style="height:1px;background:{viz.HAIR};margin:20px 0"></div>'
           f'{kicker("By discipline")}'
           f'<div style="display:flex;flex-direction:column;gap:12px;margin-top:12px">'
           f'{sport_rows}</div>' if sport_rows else "")
        + (f'<div style="height:1px;background:{viz.HAIR};margin:20px 0"></div>'
           f'{kicker("Week by week")}{charts.weekly_intensity(wk)}'
           if not wk.empty else ""))

    c = viz.status_color(tid["status"])
    by_src = tid.get("sessions_by_source") or {}
    src_line = " · ".join(f"{v} {k}" for k, v in by_src.items())
    hr_note = ""
    if hq and hq["suspect"]:
        hr_note = (
            f'<div style="margin-top:14px;padding-top:14px;'
            f'border-top:1px solid {viz.HAIR}">'
            f'{pill("sensor check", viz.STATUS["serious"])}'
            f'<div class="muted" style="margin-top:8px">'
            f'{hq["suspect"]} of {hq["runs"]} runs have a heart-rate trace that '
            f'doesn\'t hold up — wrist sensors lock onto foot-strike cadence. '
            f'Those are scored from power. Open the Streamlit app to relabel a '
            f'session by hand.</div></div>')
    rows = [("Measured", f'{tid["hours"]:.1f} h'),
            ("Score", f'{tid["score"]:.0f} / 100'),
            ("Nearest model", tid["nearest_model"])]
    # Treff's Polarization Index: a number ≥ 2.00 is a genuinely polarised block.
    # Only shown when defined (needs some time above threshold).
    pi = tid.get("polarization_index")
    if pi is not None:
        rows.append(("Polarization index",
                     f'{pi:.2f} ({"polarised" if tid["polarized"] else "not polarised"})'))
    side = card(
        f'{kicker("This window")}'
        f'<div style="display:flex;align-items:baseline;gap:8px;margin-top:10px">'
        f'<span class="display" style="font-size:40px;color:{c}">'
        f'{sh["low"]*100:.0f}%</span><span class="muted">time easy</span></div>'
        f'<div style="margin-top:10px">{pill(tid["label"], c)}</div>'
        f'<div style="display:flex;flex-direction:column;gap:8px;margin-top:16px;'
        f'font-size:13px;color:{viz.MUTED_2}">'
        + "".join(f'<div style="display:flex;justify-content:space-between">'
                  f'<span>{k}</span><span style="color:{viz.INK};font-weight:600">'
                  f'{v}</span></div>'
                  for k, v in rows)
        + f'</div><div class="muted" style="margin-top:14px">Scored from {src_line}. '
          f'Both models that hold up put ~75–80% of time easy.</div>{hr_note}',
        small=True)

    return (f'<section id="intensity"><div class="h-sec">Intensity</div>'
            f'<div class="sub">Are the easy days easy enough?</div>'
            f'<div class="grid side" style="margin-top:16px">{main}'
            f'<div>{side}</div></div></section>')


def _recovery(hb: pd.DataFrame, rhr: pd.DataFrame, slp: pd.DataFrame,
              eff: pd.DataFrame, n_runs: int) -> str:
    cur = hb.dropna(subset=["z"]) if not hb.empty else hb
    hrv_map = {"normal": ("In range", viz.STATUS["good"]),
               "suppressed": ("Suppressed", viz.STATUS["serious"]),
               "elevated": ("Elevated", "#93b356")}
    if not cur.empty:
        lbl, hc = hrv_map.get(cur.iloc[-1]["status"], ("—", viz.MUTED))
        hrv_pill = pill(f'{lbl} · z {cur.iloc[-1]["z"]:+.2f}', hc)
    else:
        hrv_pill = pill("No baseline yet", viz.MUTED)

    hrv_card = card(
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'gap:8px"><div style="font-size:13px;font-weight:700">Overnight HRV '
        f'<span style="color:{viz.MUTED};font-weight:500">· 7-day rolling</span></div>'
        f'{hrv_pill}</div><div style="margin-top:12px">{charts.hrv_chart(hb)}</div>'
        f'<div class="muted" style="margin-top:8px">Shaded band = your normal range '
        f'(±0.5 SD of a 60-day baseline). The nightly number alone is mostly noise.</div>')

    now = f'{rhr["resting_hr"].iloc[-1]:.0f}' if not rhr.empty else "—"
    roll = f'{rhr["rolling"].iloc[-1]:.1f}' if not rhr.empty else "—"
    rhr_card = card(
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<div style="font-size:13px;font-weight:700">Resting heart rate '
        f'<span style="color:{viz.MUTED};font-weight:500">· lower is better</span></div>'
        f'<span style="font-size:12.5px;color:{viz.MUTED_2}">now '
        f'<b style="color:{viz.INK}">{now}</b> · 7-day {roll}</span></div>'
        f'<div style="margin-top:12px">{charts.rhr_chart(rhr)}</div>')

    valid = slp.dropna(subset=["total_sleep_s"]) if not slp.empty else slp
    if not valid.empty:
        avg = valid["total_sleep_s"].mean()
        sleep_avg = f"{int(avg // 3600)}h {int(avg % 3600 // 60):02d}m"
    else:
        sleep_avg = "—"
    sleep_card = card(
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<div style="font-size:13px;font-weight:700">Sleep '
        f'<span style="color:{viz.MUTED};font-weight:500">· vs 8 h target</span></div>'
        f'<span style="font-size:12.5px;color:{viz.MUTED_2}">avg '
        f'<b style="color:{viz.INK}">{sleep_avg}</b></span></div>'
        f'<div style="margin-top:12px">{charts.sleep_chart(slp)}</div>'
        f'<div class="muted" style="margin-top:8px">Gaps are nights the watch '
        f'recorded nothing.</div>')

    n = int(eff["aerobic"].sum()) if not eff.empty else 0
    excluded = max(0, n_runs - len(eff))
    tail = (f" {excluded} more were left out because their heart-rate trace "
            "didn't hold up." if excluded else "")
    if eff.empty:
        note = f"No run here has a heart-rate trace worth trending.{tail}"
    elif n == 0:
        note = (f"None of the {len(eff)} usable runs were steady and aerobic "
                f"enough to compare like for like.{tail}")
    elif n < 3:
        note = (f"Only {n} of {len(eff)} usable runs qualify as steady aerobic — "
                f"too few to call a trend.{tail}")
    else:
        note = f"Higher is better — more distance held per heartbeat.{tail}"
    eff_card = card(
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<div style="font-size:13px;font-weight:700">Run efficiency '
        f'<span style="color:{viz.MUTED};font-weight:500">· steady aerobic only</span></div>'
        f'{pill(f"{n} qualifying", viz.STATUS["warning"] if n < 3 else viz.STATUS["good"])}'
        f'</div><div style="margin-top:12px">{charts.efficiency_chart(eff)}</div>'
        f'<div class="muted" style="margin-top:8px">{note}</div>')

    return (f'<section id="recovery"><div class="h-sec">Recovery</div>'
            f'<div class="sub">Is the body absorbing the work?</div>'
            f'<div class="grid g2" style="margin-top:16px">'
            f'{hrv_card}{rhr_card}{sleep_card}{eff_card}</div></section>')


def _disciplines(bal: pd.DataFrame, wv: pd.DataFrame) -> str:
    rows = ""
    for _, r in bal.iterrows():
        c = viz.SPORT_COLORS[r["sport"]]
        share = 0 if not np.isfinite(r["share"]) else r["share"] * 100
        gap = r["gap_pct"] if np.isfinite(r["gap_pct"]) else 0
        gap_c = viz.STATUS["warning"] if abs(gap) > 10 else viz.MUTED
        ds = r["days_since"]
        last = ("never recorded" if ds is None else f"stale · {ds} d" if r["stale"]
                else "today" if ds == 0 else f"{ds} d ago")
        rows += (
            f'<div class="row3 row-mix">'
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
    legend = "".join(
        f'<span style="display:flex;align-items:center;gap:6px">'
        f'<span style="width:10px;height:10px;border-radius:3px;'
        f'background:{viz.SPORT_COLORS[s]}"></span>{viz.SPORT_NAMES[s]}</span>'
        for s in viz.SPORT_ORDER)
    return ('<section id="disciplines"><div class="h-sec">Disciplines</div>'
            '<div class="sub">Is the swim / bike / run mix right for race day?</div>'
            '<div style="margin-top:16px">' + card(
                f'<div style="display:flex;justify-content:space-between;'
                f'align-items:baseline;margin-bottom:14px;gap:12px;flex-wrap:wrap">'
                f'{kicker("Share of training time · last 28 days")}'
                f'<div class="muted">target 20 / 50 / 30 · tick = target</div></div>'
                f'<div style="display:flex;flex-direction:column;gap:16px">{rows}</div>'
                f'<div style="height:1px;background:{viz.HAIR};margin:20px 0"></div>'
                f'<div style="display:flex;justify-content:space-between;'
                f'align-items:baseline;margin-bottom:8px;gap:12px;flex-wrap:wrap">'
                f'{kicker("Weekly hours by sport")}'
                f'<div style="display:flex;gap:14px;font-size:12px;'
                f'color:{viz.MUTED_2}">{legend}</div></div>'
                f'{charts.volume_chart(wv)}')
            + "</div></section>")


# ---------------------------------------------------------------------------

def render(weeks: int = 16, through: pd.Timestamp | None = None,
           show_sync: bool = True) -> str:
    """Build the whole page. Reads the database; makes no network calls."""
    db.init_db()
    activities = db.load_activities()
    daily = db.load_daily()
    sleep = db.load_sleep()
    strength = db.load_strength_sessions()
    tags = db.load_session_tags()
    asof = pd.Timestamp(through).normalize() if through is not None \
        else pd.Timestamp(date.today())
    cutoff = asof - pd.Timedelta(weeks=weeks)

    if activities.empty and daily.empty:
        body = card('<div class="muted">No data yet. Run '
                    '<code>python track.py sync</code> locally, or hit Sync above '
                    'once Garmin tokens are configured.</div>')
        return _page(_header(None, None, show_sync) + body)

    acts = activities[activities["date"] >= cutoff]
    day = daily[daily["date"] >= cutoff] if not daily.empty else daily
    slp = sleep[sleep["date"] >= cutoff] if not sleep.empty else sleep

    last_any = max([pd.to_datetime(df["date"]).max()
                    for df in (activities, daily) if not df.empty], default=pd.NaT)
    stale = int((asof - last_any).days) if pd.notna(last_any) else None

    race = None
    if settings.race_date:
        try:
            rd = datetime.strptime(settings.race_date, "%Y-%m-%d").date()
            days_to = (rd - asof.date()).days
            if days_to >= 0:
                race = {"days": days_to, "name": settings.race_name}
        except ValueError:
            pass

    ff_all = metrics.fitness_fatigue(activities, through=asof)
    ff = ff_all[ff_all["date"] >= cutoff]
    ms_all = metrics.monotony_strain(activities, through=asof)

    meta = (f"{len(activities)} activities · {len(daily)} wellness days · "
            f"{charts._short_day(activities['date'].min())} – "
            f"{charts._short_day(last_any)} · generated {asof:%d %b %Y}")

    body = (
        _header(stale, race, show_sync)
        + _flags(metrics.training_flags(activities, daily, sleep, through=asof,
                                        tags=tags))
        + _overview(metrics.progression_score(
            activities, daily, sleep,
            target_sessions=settings.weekly_session_target,
            strength=strength, through=asof, tags=tags),
            metrics.race_projection(activities, settings.race_date, through=asof)
            if settings.race_date else None)
        + _load(ff, metrics.ramp_rate(ff_all),
                ms_all[ms_all["date"] >= cutoff])
        + _intensity(metrics.intensity_summary(acts, source="auto", tags=tags),
                     metrics.intensity_distribution(acts, by_sport=True,
                                                    source="auto", tags=tags),
                     metrics.intensity_distribution(acts, source="auto", tags=tags),
                     metrics.hr_quality_summary(acts, tags=tags))
        + _recovery(metrics.hrv_baseline(daily).pipe(
                        lambda d: d[d["date"] >= cutoff] if not d.empty else d),
                    metrics.rolling_metric(day, "resting_hr"), slp,
                    metrics.run_efficiency(acts),
                    int((acts["sport"] == "run").sum()))
        + _disciplines(metrics.discipline_balance(activities, through=asof),
                       metrics.weekly_volume(acts))
        + f'<div style="display:flex;justify-content:space-between;gap:16px;'
          f'flex-wrap:wrap;font-size:12px;color:{viz.MUTED};margin-top:40px;'
          f'padding-top:18px;border-top:1px solid {viz.HAIR}">'
          f'<span>{meta}</span>'
          f'<span>Progression · a personal training tracker</span></div>')
    return _page(body)


def _page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="{viz.BG}">
<title>Progression · Ironman Tracker</title>
<link rel="icon" href="data:image/svg+xml,{_FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Caprasimo&family=Figtree:wght@400;500;600;700;800&display=swap">
<style>{_CSS}</style>
</head><body><div class="wrap">{body}</div>
<script>{_JS}</script>
</body></html>"""
