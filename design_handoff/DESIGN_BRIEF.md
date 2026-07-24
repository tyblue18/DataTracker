# Ironman Training Tracker — Design Brief for Claude Design

Hand this file (and `sample_data.json` in the same folder) to Claude Design at
claude.ai/design. It describes a dashboard to design; real sample data is
attached so you can lay out actual numbers, not placeholders.

---

## 1. What this is

A personal dashboard for an athlete training for an **Ironman triathlon**
(swim + bike + run). It reads their Garmin watch data and answers one question
at a glance: **"Am I actually progressing?"** — then lets them drill into why.

- **User:** one committed endurance athlete (not a team, not coaches). Data-literate.
- **Tone:** focused, sporty, calm. Think a premium fitness product (WHOOP, Oura,
  Garmin, TrainingPeaks) — not a corporate BI tool. Dark mode should look great.
- **Platform:** primarily desktop web, but a clean responsive/mobile layout matters
  (they'll check it on a phone after workouts).

## 2. The hero: Progression Score (design this first)

A single **0–100 score**, updated daily, that says whether they're improving. It
must be the first thing the eye lands on. It comes with:

- The **number** (0–100) and a **band label**: Regressing / Stalling / Maintaining /
  Productive / Thriving.
- A **14-day trend delta** (e.g. "▲ +16.5") — is the score itself rising or falling?
- A **one-line plain-English verdict**, e.g. *"Productive — training is consistent,
  but efficiency is fading."*
- A **sparkline / trend line** of the score over time (the number is the latest point).
- **Five pillar sub-scores** (each 0–100), shown as cards or a radial breakdown:
  - 🏋️ **Fitness** — is training load ramping up?
  - ⚡ **Efficiency** — faster at the same heart rate?
  - 🌙 **Recovery** — HRV vs the athlete's own normal range, sleep, freshness?
  - 📅 **Consistency** — enough sessions, no long gaps?
  - 🎚️ **Intensity** — is the easy/hard split near a model that works?

Any pillar can be **unavailable** ("—") when the underlying data isn't there.
That state needs a design: it must read as "not measured", clearly distinct from
"measured and bad". Design the score so a weak pillar is visually obvious, and so
the headline number visibly rests on fewer pillars when some are missing.

**Color semantics — keep consistent everywhere:** green = good/improving,
amber = caution/flat, red = declining/risk. Bands map: 80+ green, 65–79 light green,
50–64 amber, 35–49 orange, <35 red.

## 3. Screens / sections (in priority order)

0. **Alert strip** — the handful of conditions worth interrupting for (stale data,
   inverted intensity split, detraining, a neglected discipline, suppressed HRV),
   each with the number behind it. Sits above everything, ranked by severity.
1. **Overview** — the Progression Score hero + five pillars + a race countdown badge
   ("⏳ 87 days to Ironman Wales") + a race-day fitness/form projection. Landing view.
2. **Training Load** — Fitness (CTL, 42-day load) and Fatigue (ATL, 7-day load) as
   lines over a daily-load bar chart, with Form (TSB) as a separate diverging band
   below sharing the x-axis. Plus a ramp-rate tile and a training-monotony chart.
   The first 42 days must be visibly marked as model warm-up.
3. **Intensity** — the athlete's low/moderate/high split as a 100% stacked row,
   directly above the same row for the polarised and pyramidal models. Then the
   split per discipline, then week by week.
4. **Recovery** — overnight HRV plotted against a shaded personal normal range
   (±0.5 SD of a 60-day baseline), resting HR trend (lower = better), sleep
   duration with an 8-hour target line, and run efficiency **restricted to steady
   aerobic sessions** (with an explicit empty state when none qualify).
5. **Disciplines** — share of training time per sport against a 20/50/30 target,
   days since last session per sport, and weekly hours/distance stacked bars.

A tabbed or single-scroll layout are both fine — you choose what reads best. Avoid
one endless wall of charts; group by the question each answers.

## 4. Data model (what's available)

**`sample_data.json` is real data, not placeholders**, exported straight from the
athlete's database. Regenerate it any time with `python track.py handoff` — do not
hand-edit it, and do not invent nicer numbers. Its top-level keys map one-to-one
onto the sections above:

| Key | Feeds |
|---|---|
| `meta` | data span, staleness, race name/date, record counts |
| `flags` | the alert strip — `{status, title, detail}`, pre-ranked |
| `progression_score` / `progression_series` | the hero + its sparkline |
| `fitness_fatigue` / `ramp_rate` / `monotony` | the Training Load section |
| `intensity_summary` / `intensity_models` / `intensity_by_sport` / `intensity_weekly` | the Intensity section |
| `hrv_baseline` / `resting_hr` / `sleep` / `run_efficiency` | the Recovery section |
| `discipline_balance` / `discipline_target` / `weekly_volume` | the Disciplines section |
| `race_projection` | the race-day projection tiles |

### States the design must handle

Real data is lumpy, and these are not edge cases — they are all live in the
current export at some point in its history:

- **A pillar reading `null`** — not measured, and visually distinct from a bad score.
- **`progression_score` itself `null`** — too little history to score at all.
- **`days_stale > 0`** — every figure is computed as of today, so a stale database
  reads as time off. The alert strip says so; the design must not bury it.
- **`run_efficiency` empty after filtering** — no qualifying steady aerobic runs.
  Needs a real empty state that explains itself, not a blank card.
- **`warmup: true` rows** in `fitness_fatigue` — the first 42 days, where the model
  is still filling from its seed and the curve should be visibly discounted.
- **A discipline with `stale: true`** or a share far from target.

**Do NOT feature these — they're sparse or empty in the real data:** VO₂max
(updates rarely, nearly flat), cycling VO₂max (empty), body weight (empty). A tiny
stat chip for VO₂max is fine; no dedicated charts.

## 4b. Colour

`garmin_tracker/viz.py` holds the current tokens, already validated for
colour-vision deficiency separation and contrast against the dark surface. Reuse
them, or if you re-pick, hold the replacement to the same bar:

- Sport hues are **identity** — swim/bike/run keep their colour in every chart.
- Status (good/warning/serious/critical) is **reserved** and never doubles as a
  series colour.
- Intensity bands are an **ordered ramp** (one hue, monotone lightness), not three
  unrelated hues, and not the status colours.
- No dual-axis charts, ever.

## 5. Design priorities (what "good" means here)

1. **One glance answers "am I progressing?"** — the score and its direction dominate.
2. **Trends over snapshots** — always show direction/rolling averages, not just today.
3. **Consistent color language** — green/amber/red means the same thing everywhere.
4. **Reference context** — target lines, personal baselines, "good/bad" direction hints.
5. **Progressive disclosure** — headline first; detailed charts below/behind tabs.
6. **Beautiful, motivating, not clinical** — this should make them want to train.

## 6. Out of scope

No data entry, no login/settings screens, no social features. This is a personal,
read-only analytics dashboard. Design the presentation of the data described above.

## 7. If you are implementing, not just designing

The app is Streamlit + Plotly; `dashboard.py` is the current implementation and
`garmin_tracker/metrics.py` computes every number in the export. Two rules:

- **Do not recompute metrics in the view layer.** If a number isn't in
  `metrics.py`, add it there with a test in `tests/`, then render it.
- **Don't invent thresholds.** The bands in the brief (intensity models, HRV
  smallest worthwhile change, ramp rate, taper form) come from the endurance
  literature and are documented at their definitions in `metrics.py`.
