"""Hand-built SVG charts.

Each function takes a tidy DataFrame and returns an SVG string sized by
``viewBox`` so it scales to whatever column it is dropped into. Geometry follows
the approved design: hairline grids one shade off the surface, thin marks,
selective direct labels, and no chart-library chrome.
"""

from __future__ import annotations

import html
from datetime import datetime

import numpy as np
import pandas as pd

from . import viz


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _svg(w: float, h: float, body: str) -> str:
    # Sizing lives in CSS, not in the SVG attributes: height="auto" is not a
    # valid SVG length and browsers reject it. The viewBox plus width:100% is
    # what makes these scale to their container.
    return (f'<svg viewBox="0 0 {w:g} {h:g}" preserveAspectRatio="xMidYMid meet" '
            f'style="display:block;width:100%;height:auto;overflow:visible">'
            f'{body}</svg>')


def _text(x, y, s, *, fill=viz.MUTED, size=11, anchor="start", weight=400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
            f'font-family="Figtree,sans-serif" font-weight="{weight}" '
            f'text-anchor="{anchor}">{_esc(s)}</text>')


def _line(x1, y1, x2, y2, stroke, width=1, dash=None) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"{d}/>')


def _path(points, stroke, width, fill="none") -> str:
    if not points:
        return ""
    d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (f'<path d="{d}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linejoin="round"/>')


def _area(points, y0, fill) -> str:
    if not points:
        return ""
    d = ("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in points) +
         f"L{points[-1][0]:.1f},{y0:.1f}L{points[0][0]:.1f},{y0:.1f}Z")
    return f'<path d="{d}" fill="{fill}"/>'


def _rect(x, y, w, h, fill, rx=2) -> str:
    if h <= 0 or w <= 0:
        return ""
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" rx="{rx}"/>')


def _month_ticks(dates) -> list[tuple[int, str]]:
    """Index + label for the first of each month — a calendar-anchored axis."""
    out = []
    for i, d in enumerate(pd.to_datetime(pd.Series(list(dates)))):
        if d.day == 1:
            out.append((i, d.strftime("%b")))
    return out


def _fmt_day(d) -> str:
    return pd.Timestamp(d).strftime("%-d %b") if hasattr(datetime, "strftime") \
        else pd.Timestamp(d).strftime("%d %b")


def _short_day(d) -> str:
    ts = pd.Timestamp(d)
    return f"{ts.day} {ts.strftime('%b')}"


def empty(msg: str, h: int = 120) -> str:
    return _svg(400, h, _text(200, h / 2, msg, anchor="middle", size=12))


# ---------------------------------------------------------------------------

def progression_spark(series: pd.DataFrame, color: str,
                      w: int = 700, h: int = 205) -> str:
    """Score over time: filled area, gridlines at the band edges, endpoint label."""
    s = series.dropna(subset=["score"])
    if len(s) < 2:
        return empty("Not enough history yet", h)
    L, R, T, B = 30, 46, 10, 20
    n = len(s)
    xs = lambda i: L + (w - L - R) * i / (n - 1)
    ys = lambda v: T + (h - T - B) * (1 - v / 100)

    out = []
    for g in (80, 65, 50, 35):
        out.append(_line(L, ys(g), w - R, ys(g), "rgba(240,230,210,.07)"))
        out.append(_text(L - 6, ys(g) + 4, g, anchor="end", size=10))

    pts = [(xs(i), ys(v)) for i, v in enumerate(s["score"])]
    out.append(_area(pts, ys(0), "rgba(147,179,86,.10)"))
    out.append(_path(pts, color, 2.5))

    lx, ly = pts[-1]
    out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{color}" '
               f'stroke="{viz.BG}" stroke-width="2"/>')
    out.append(_text(lx + 9, ly + 4, round(float(s["score"].iloc[-1])),
                     fill=color, size=13, weight=700))
    for i, lbl in _month_ticks(s["date"]):
        out.append(_text(xs(i), h - 4, lbl, anchor="middle"))
    return _svg(w, h, "".join(out))


def load_chart(ff: pd.DataFrame, w: int = 940, h: int = 300) -> str:
    """Fitness and fatigue lines over daily-load bars, with the warm-up marked."""
    if ff.empty:
        return empty("No training-load data in range", h)
    L, R, T, B = 34, 8, 16, 20
    n = len(ff)
    top = max(60.0, float(np.nanmax([ff["load"].max(), ff["ctl"].max(),
                                     ff["atl"].max()])) * 1.12)
    xs = lambda i: L + (w - L - R) * i / max(1, n - 1)
    ys = lambda v: T + (h - T - B) * (1 - v / top)

    out = []
    warm = ff["warmup"].fillna(False).to_numpy()
    if warm.any():
        end = int(np.max(np.nonzero(warm)))
        out.append(_rect(L, T, xs(end) - L, h - T - B, "rgba(240,230,210,.045)", rx=0))
        out.append(_line(xs(end), T, xs(end), h - B, "rgba(240,230,210,.2)", 1, "3 4"))
        out.append(_text((L + xs(end)) / 2, T + 12, "model warm-up · first 42 days",
                         anchor="middle", size=10.5, fill="rgba(240,230,210,.4)"))

    step = 50 if top <= 260 else 100
    g = 0
    while g <= top:
        out.append(_line(L, ys(g), w - R, ys(g), viz.GRID))
        out.append(_text(L - 6, ys(g) + 4, g, anchor="end", size=10))
        g += step

    bw = max(1.0, (w - L - R) / n * 0.55)
    for i, v in enumerate(ff["load"].fillna(0)):
        if v > 0:
            out.append(_rect(xs(i) - bw / 2, ys(v), bw, ys(0) - ys(v),
                             viz.LOAD_BAR, rx=1.5))
    out.append(_path([(xs(i), ys(v)) for i, v in enumerate(ff["atl"])], viz.ATL, 2))
    out.append(_path([(xs(i), ys(v)) for i, v in enumerate(ff["ctl"])], viz.CTL, 2.8))
    for i, lbl in _month_ticks(ff["date"]):
        out.append(_text(xs(i), h - 4, lbl, anchor="middle"))
    return _svg(w, h, "".join(out))


def tsb_chart(ff: pd.DataFrame, w: int = 940, h: int = 130) -> str:
    """Form as a diverging band: above zero fresh, below zero fatigued."""
    s = ff.dropna(subset=["tsb"])
    if s.empty:
        return empty("No form data yet", h)
    L, R = 34, 8
    n = len(s)
    lo = min(-10.0, float(s["tsb"].min()) * 1.15)
    hi = max(10.0, float(s["tsb"].max()) * 1.15)
    xs = lambda i: L + (w - L - R) * i / max(1, n - 1)
    ys = lambda v: 10 + (h - 24) * (1 - (v - lo) / (hi - lo))

    vals = list(s["tsb"])
    out = [_line(L, ys(0), w - R, ys(0), "rgba(240,230,210,.25)")]
    out.append(_area([(xs(i), ys(max(0.0, v))) for i, v in enumerate(vals)],
                     ys(0), viz.TSB_POS))
    out.append(_area([(xs(i), ys(min(0.0, v))) for i, v in enumerate(vals)],
                     ys(0), viz.TSB_NEG))
    out.append(_path([(xs(i), ys(v)) for i, v in enumerate(vals)], viz.TSB_LINE, 1.6))
    out.append(_text(L - 6, ys(0) + 4, "0", anchor="end", size=10))
    for g in (round(lo / 10) * 10, round(hi / 10) * 10):
        if abs(g) > 5:
            out.append(_text(L - 6, ys(g) + 4, int(g), anchor="end", size=10))
    return _svg(w, h, "".join(out))


def monotony_chart(m: pd.DataFrame, w: int = 320, h: int = 90) -> str:
    """Line broken wherever monotony is undefined — gaps are not zeros."""
    if m.empty or m["monotony"].dropna().empty:
        return empty("Not enough training days", h)
    L, R, T, B = 6, 6, 8, 8
    n = len(m)
    top = max(2.2, float(m["monotony"].max()) * 1.15)
    xs = lambda i: L + (w - L - R) * i / max(1, n - 1)
    ys = lambda v: T + (h - T - B) * (1 - v / top)

    out = [_line(L, ys(2.0), w - R, ys(2.0), "rgba(240,230,210,.12)", 1, "3 4")]
    seg: list[tuple[float, float]] = []
    for i, v in enumerate(m["monotony"]):
        if pd.isna(v):
            if len(seg) > 1:
                out.append(_path(seg, viz.CTL, 2))
            seg = []
        else:
            seg.append((xs(i), ys(v)))
    if len(seg) > 1:
        out.append(_path(seg, viz.CTL, 2))
    if seg:
        out.append(f'<circle cx="{seg[-1][0]:.1f}" cy="{seg[-1][1]:.1f}" r="3.5" '
                   f'fill="{viz.CTL}"/>')
    return _svg(w, h, "".join(out))


def weekly_intensity(wk: pd.DataFrame, w: int = 1060, h: int = 230) -> str:
    """Week-by-week 100% stacked bands."""
    if wk.empty:
        return empty("No zoned sessions in range", h)
    L, R, T, B = 34, 8, 8, 22
    n = len(wk)
    cw = (w - L - R) / n
    bw = cw * 0.62
    plot = h - T - B

    out = []
    for g in (0, 50, 100):
        gy = T + plot * (1 - g / 100)
        out.append(_line(L, gy, w - R, gy, viz.GRID))
        out.append(_text(L - 6, gy + 4, f"{g}%", anchor="end", size=10))
    for i, (_, r) in enumerate(wk.iterrows()):
        cx = L + cw * i + cw / 2
        acc = 0.0
        for band in ("low", "moderate", "high"):
            pct = float(r.get(f"{band}_pct") or 0)
            bh = plot * pct / 100
            out.append(_rect(cx - bw / 2, T + plot - acc - bh, bw, bh,
                             viz.BAND_COLORS[band]))
            acc += bh
        if i % 2 == 0:
            out.append(_text(cx, h - 5, _short_day(r["period_start"]),
                             anchor="middle", size=10))
    return _svg(w, h, "".join(out))


def hrv_chart(hb: pd.DataFrame, w: int = 540, h: int = 205) -> str:
    """7-day mean against the personal normal band, labelled in milliseconds."""
    s = hb.dropna(subset=["roll"])
    if s.empty:
        return empty("Not enough HRV history", h)
    L, R, T, B = 42, 8, 8, 18
    n = len(s)
    vals = pd.concat([s["ln_hrv"], s["lower"], s["upper"]]).dropna()
    lo, hi = float(vals.min()) - 0.04, float(vals.max()) + 0.04
    xs = lambda i: L + (w - L - R) * i / max(1, n - 1)
    ys = lambda v: T + (h - T - B) * (1 - (v - lo) / (hi - lo))

    out = []
    band = s.dropna(subset=["lower", "upper"])
    if not band.empty:
        idx = [s.index.get_loc(i) for i in band.index]
        up = [(xs(i), ys(v)) for i, v in zip(idx, band["upper"], strict=False)]
        dn = [(xs(i), ys(v)) for i, v in zip(idx, band["lower"], strict=False)][::-1]
        d = ("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in up + dn) + "Z")
        out.append(f'<path d="{d}" fill="rgba(138,154,106,.22)"/>')

    for i, v in enumerate(s["ln_hrv"]):
        if pd.notna(v) and lo <= v <= hi:
            out.append(f'<circle cx="{xs(i):.1f}" cy="{ys(v):.1f}" r="1.8" '
                       f'fill="rgba(240,230,210,.28)"/>')
    pts = [(xs(i), ys(v)) for i, v in enumerate(s["roll"])]
    out.append(_path(pts, viz.CTL, 2.4))
    out.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="4" '
               f'fill="{viz.CTL}" stroke="{viz.BG}" stroke-width="2"/>')
    # Ticks in ms, because nobody thinks in log-milliseconds.
    for ms in (20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 90, 100, 120):
        v = float(np.log(ms))
        if lo <= v <= hi:
            out.append(_text(L - 6, ys(v) + 3, f"{ms} ms", anchor="end", size=9.5))
    for i, lbl in _month_ticks(s["date"]):
        out.append(_text(xs(i), h - 3, lbl, anchor="middle", size=10))
    return _svg(w, h, "".join(out))


def rhr_chart(rhr: pd.DataFrame, w: int = 540, h: int = 205) -> str:
    if rhr.empty:
        return empty("No resting-HR data in range", h)
    L, R, T, B = 30, 8, 8, 18
    n = len(rhr)
    lo = float(rhr["resting_hr"].min()) - 3
    hi = float(rhr["resting_hr"].max()) + 3
    xs = lambda i: L + (w - L - R) * i / max(1, n - 1)
    ys = lambda v: T + (h - T - B) * (1 - (v - lo) / (hi - lo))

    out = []
    for g in range(int(lo // 5 * 5) + 5, int(hi) + 1, 5):
        out.append(_line(L, ys(g), w - R, ys(g), viz.GRID))
        out.append(_text(L - 5, ys(g) + 3, g, anchor="end", size=9.5))
    out.append(_path([(xs(i), ys(v)) for i, v in enumerate(rhr["resting_hr"])],
                     "rgba(240,230,210,.22)", 1.2))
    out.append(_path([(xs(i), ys(v)) for i, v in enumerate(rhr["rolling"])],
                     viz.CTL, 2.4))
    for i, lbl in _month_ticks(rhr["date"]):
        out.append(_text(xs(i), h - 3, lbl, anchor="middle", size=10))
    return _svg(w, h, "".join(out))


def sleep_chart(sleep: pd.DataFrame, w: int = 540, h: int = 205) -> str:
    s = sleep.dropna(subset=["total_sleep_s"])
    if s.empty:
        return empty("No sleep data in range", h)
    L, R, T, B = 26, 8, 8, 18
    n = len(sleep)
    top = 10.0
    cw = (w - L - R) / max(1, n)
    ys = lambda v: T + (h - T - B) * (1 - v / top)

    out = []
    for g in (4, 8):
        out.append(_text(L - 5, ys(g) + 3, f"{g}h", anchor="end", size=9.5))
    for i, (_, r) in enumerate(sleep.iterrows()):
        v = r["total_sleep_s"]
        if pd.isna(v):
            continue
        hrs = v / 3600.0
        out.append(_rect(L + cw * i + cw * 0.15, ys(hrs), cw * 0.7,
                         ys(0) - ys(hrs),
                         "#8a9a6a" if hrs >= 7 else "#8a7f6c", rx=1.5))
    out.append(_line(L, ys(8), w - R, ys(8), viz.CTL, 1.4, "5 5"))
    for i, lbl in _month_ticks(sleep["date"]):
        out.append(_text(L + cw * i, h - 3, lbl, anchor="middle", size=10))
    return _svg(w, h, "".join(out))


def efficiency_chart(eff: pd.DataFrame, w: int = 540, h: int = 205) -> str:
    """Qualifying aerobic runs highlighted and labelled; the rest kept faint."""
    if eff.empty:
        return empty("No runs with pace and heart rate", h)
    L, R, T, B = 30, 12, 16, 18
    d0 = pd.Timestamp(eff["date"].min()).value
    d1 = pd.Timestamp(eff["date"].max()).value
    span = max(1, d1 - d0)
    xs = lambda d: L + (w - L - R) * (pd.Timestamp(d).value - d0) / span
    lo = float(eff["efficiency"].min()) * 0.9
    hi = float(eff["efficiency"].max()) * 1.1
    ys = lambda v: T + (h - T - B) * (1 - (v - lo) / (hi - lo))

    out = []
    for g in np.linspace(lo, hi, 4)[1:-1]:
        out.append(_line(L, ys(g), w - R, ys(g), viz.GRID))
        out.append(_text(L - 5, ys(g) + 3, f"{g:.2f}", anchor="end", size=9.5))
    for _, r in eff[~eff["aerobic"]].iterrows():
        out.append(f'<circle cx="{xs(r["date"]):.1f}" cy="{ys(r["efficiency"]):.1f}" '
                   f'r="2.2" fill="rgba(240,230,210,.16)"/>')
    aero = eff[eff["aerobic"]]
    for _, r in aero.iterrows():
        cx, cy = xs(r["date"]), ys(r["efficiency"])
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5.5" fill="{viz.ACCENT}" '
                   f'stroke="{viz.INK}" stroke-width="1.5"/>')
        if len(aero) <= 8:
            out.append(_text(cx, cy - 11, f'{r["efficiency"]:.2f}', anchor="middle",
                             fill=viz.ACCENT_2, size=11, weight=700))
            out.append(_text(cx, h - 3, _short_day(r["date"]), anchor="middle",
                             size=9.5))
    return _svg(w, h, "".join(out))


def volume_chart(wv: pd.DataFrame, w: int = 1060, h: int = 240) -> str:
    """Weekly hours stacked by discipline."""
    if wv.empty:
        return empty("No activities in range", h)
    weeks = sorted(wv["week_start"].unique())
    n = len(weeks)
    L, R, T, B = 30, 8, 10, 22
    plot = h - T - B
    totals = [float(wv[wv["week_start"] == wk]["hours"].sum()) for wk in weeks]
    top = max(1.0, max(totals)) * 1.1
    cw = (w - L - R) / n
    bw = cw * 0.56
    ys = lambda v: T + plot * (1 - v / top)

    out = []
    step = 2 if top <= 10 else 4
    g = 0
    while g <= top:
        out.append(_line(L, ys(g), w - R, ys(g), viz.GRID))
        out.append(_text(L - 5, ys(g) + 3, f"{g}h", anchor="end", size=10))
        g += step
    for i, wk in enumerate(weeks):
        cx = L + cw * i + cw / 2
        acc = 0.0
        for sport in viz.SPORT_ORDER:
            row = wv[(wv["week_start"] == wk) & (wv["sport"] == sport)]
            if row.empty:
                continue
            bh = plot * float(row["hours"].iloc[0]) / top
            out.append(_rect(cx - bw / 2, ys(0) - acc - bh, bw, bh,
                             viz.SPORT_COLORS[sport]))
            acc += bh
        if n <= 20 or i % 2 == 0:
            out.append(_text(cx, h - 5, _short_day(wk), anchor="middle", size=10))
    return _svg(w, h, "".join(out))
