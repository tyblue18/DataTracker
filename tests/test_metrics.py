"""Tests for the metrics layer.

Focus is on the failure modes that produce a *plausible wrong number* rather
than an exception — those are the ones a dashboard will happily render.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from garmin_tracker import metrics


def make_activities(rows) -> pd.DataFrame:
    """rows: list of (date, sport, load[, duration_s, avg_hr, avg_speed_mps]).

    activity_id is derived from the date so that frames built by separate calls
    can be concatenated without colliding — activity_id is the real primary key
    and tag merges join on it.
    """
    recs = []
    for i, r in enumerate(rows):
        d, sport, load = r[0], r[1], r[2]
        recs.append({
            "activity_id": int(pd.Timestamp(d).strftime("%Y%m%d")) * 10 + i,
            "date": pd.Timestamp(d), "sport": sport,
            "training_load": load,
            "duration_s": r[3] if len(r) > 3 else 3600.0,
            "avg_hr": r[4] if len(r) > 4 else 140.0,
            "avg_speed_mps": r[5] if len(r) > 5 else 3.0,
            "distance_m": 10000.0,
            **{f"hr_z{z}_s": np.nan for z in range(1, 6)},
        })
    return pd.DataFrame(recs)


def with_zones(df: pd.DataFrame, zones: dict[int, float],
               prefix: str = "hr") -> pd.DataFrame:
    out = df.copy()
    for z, secs in zones.items():
        out[f"{prefix}_z{z}_s"] = secs
    return out


def cadence_locked_run(date="2026-01-01", hr=172.0, cadence=173.0):
    """A run where HR sits on cadence and contradicts power.

    HR reports most of the session above threshold; power reports almost none
    of it — the signature of an optical sensor tracking foot strike.
    """
    df = make_activities([(date, "run", 100, 3600, hr, 3.0)])
    df["avg_cadence"] = cadence
    df = with_zones(df, {1: 100, 2: 200, 3: 300, 4: 2500, 5: 500}, "hr")
    return with_zones(df, {1: 600, 2: 2400, 3: 400, 4: 200, 5: 0}, "pwr")


# ---------------------------------------------------------------------------
# daily_load / fitness_fatigue
# ---------------------------------------------------------------------------

def test_daily_load_extends_past_last_activity():
    """Rest days after the final workout must exist, or fitness never decays."""
    acts = make_activities([("2026-01-01", "run", 100)])
    dl = metrics.daily_load(acts, through=pd.Timestamp("2026-01-31"))
    assert dl["date"].max() == pd.Timestamp("2026-01-31")
    assert len(dl) == 31
    assert dl["load"].iloc[1:].eq(0).all()


def test_fitness_decays_during_a_layoff():
    """The regression this guards: CTL frozen at the last workout.

    Ending the series at the last activity reports the fitness you had three
    weeks ago as the fitness you have today.
    """
    acts = make_activities([(f"2026-01-{d:02d}", "run", 100) for d in range(1, 29)])
    at_last = metrics.fitness_fatigue(acts, through=pd.Timestamp("2026-01-28"))
    later = metrics.fitness_fatigue(acts, through=pd.Timestamp("2026-02-28"))

    assert later["date"].max() == pd.Timestamp("2026-02-28")
    assert later["ctl"].iloc[-1] < at_last["ctl"].iloc[-1] * 0.75
    # Fatigue decays far faster than fitness, so form must swing positive.
    assert later["tsb"].iloc[-1] > 0


def test_fitness_fatigue_defaults_to_today():
    acts = make_activities([("2026-01-01", "run", 100)])
    ff = metrics.fitness_fatigue(acts)
    assert ff["date"].max() == pd.Timestamp(pd.Timestamp.today().date())


def test_ctl_seed_is_not_day_ones_load():
    """A single big day one must not anchor the whole fitness curve."""
    rows = [("2026-01-01", "run", 500)] + \
           [(f"2026-01-{d:02d}", "run", 50) for d in range(2, 15)]
    ff = metrics.fitness_fatigue(make_activities(rows),
                                 through=pd.Timestamp("2026-01-14"))
    # pandas' adjust=False would seed CTL at 500 here.
    assert ff["ctl"].iloc[0] < 200


def test_warmup_flagged_for_first_42_days():
    acts = make_activities([(f"2026-01-{d:02d}", "run", 60) for d in range(1, 29)])
    ff = metrics.fitness_fatigue(acts, through=pd.Timestamp("2026-06-01"))
    assert ff["warmup"].iloc[:metrics.CTL_WARMUP_DAYS].all()
    assert not ff["warmup"].iloc[metrics.CTL_WARMUP_DAYS:].any()


def test_ramp_rate_is_none_while_warming_up():
    acts = make_activities([(f"2026-01-{d:02d}", "run", 60) for d in range(1, 15)])
    assert metrics.ramp_rate(metrics.fitness_fatigue(
        acts, through=pd.Timestamp("2026-01-14"))) is None


def test_ramp_rate_reports_detraining():
    acts = make_activities([(f"2026-0{m}-{d:02d}", "run", 80)
                            for m in (1, 2) for d in range(1, 28)])
    rr = metrics.ramp_rate(metrics.fitness_fatigue(
        acts, through=pd.Timestamp("2026-04-15")))
    assert rr is not None and rr["per_week"] < 0 and rr["label"] == "Detraining"


def test_empty_activities_are_handled():
    empty = make_activities([]).iloc[0:0]
    assert metrics.fitness_fatigue(empty).empty
    assert metrics.daily_load(empty).empty
    assert metrics.intensity_summary(empty) is None
    assert metrics.discipline_balance(empty).empty


# ---------------------------------------------------------------------------
# Intensity distribution
# ---------------------------------------------------------------------------

def test_intensity_summary_classifies_inverted():
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 100, 2: 100, 3: 300, 4: 400, 5: 100})
    tid = metrics.intensity_summary(acts)
    assert tid["label"] == "Inverted"
    assert tid["status"] == "critical"
    assert tid["shares"]["high"] > tid["shares"]["low"]


def test_intensity_summary_classifies_polarised():
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 400, 2: 400, 3: 50, 4: 100, 5: 50})
    assert metrics.intensity_summary(acts)["label"] == "Polarised"


def test_intensity_summary_classifies_pyramidal():
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 400, 2: 380, 3: 190, 4: 30, 5: 0})
    assert metrics.intensity_summary(acts)["label"] == "Pyramidal"


def test_polarization_index_flags_the_polarised_model():
    # Same 80/5/15-ish split the classifier calls "Polarised".
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 400, 2: 400, 3: 50, 4: 100, 5: 50})
    tid = metrics.intensity_summary(acts)
    assert tid["polarization_index"] >= metrics.POLARIZATION_THRESHOLD
    assert tid["polarized"] is True


def test_polarization_index_below_two_for_pyramidal():
    # A pyramidal split scores below the 2.0 line despite a big easy base.
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 400, 2: 380, 3: 190, 4: 30, 5: 0})
    tid = metrics.intensity_summary(acts)
    assert tid["polarization_index"] < metrics.POLARIZATION_THRESHOLD
    assert tid["polarized"] is False


def test_polarization_index_undefined_without_high_intensity():
    # No time above threshold: the index is not defined (a sub-threshold block
    # is "not polarised", which the qualitative label already conveys).
    acts = with_zones(make_activities([("2026-01-01", "run", 100)]),
                      {1: 400, 2: 400, 3: 200, 4: 0, 5: 0})
    tid = metrics.intensity_summary(acts)
    assert tid["polarization_index"] is None
    assert tid["polarized"] is False


def test_published_models_score_well():
    for low, _mod, high in metrics.TID_MODELS.values():
        assert metrics.intensity_score(low, high) >= 88


def test_inverted_distribution_scores_badly():
    assert metrics.intensity_score(0.27, 0.41) < 25


def test_activities_without_zone_data_are_excluded_not_zeroed():
    """A missing zone breakdown must not be read as 'all easy'."""
    acts = make_activities([("2026-01-01", "run", 100),
                            ("2026-01-02", "run", 100)])
    acts = acts.copy()
    for z, secs in {1: 600, 2: 600, 3: 0, 4: 0, 5: 0}.items():
        acts.loc[0, f"hr_z{z}_s"] = secs   # row 1 has zones, row 2 does not
    tid = metrics.intensity_summary(acts)
    assert tid["hours"] == pytest.approx(1200 / 3600)


def test_has_zone_data():
    plain = make_activities([("2026-01-01", "run", 100)])
    assert not metrics.has_zone_data(plain)
    assert metrics.has_zone_data(with_zones(plain, {1: 600}))


# ---------------------------------------------------------------------------
# Heart-rate quality (optical cadence lock)
# ---------------------------------------------------------------------------

def test_cadence_locked_run_is_flagged():
    q = metrics.hr_quality(cadence_locked_run())
    assert q["hr_suspect"].iloc[0]
    assert q["hr_quality"].iloc[0] == "suspect"
    assert q["hr_cadence_close"].iloc[0]
    assert q["hr_power_disagree"].iloc[0]


def test_clean_run_is_not_flagged():
    """Easy HR well below cadence, and HR agreeing with power."""
    df = make_activities([("2026-01-01", "run", 60, 3600, 135.0, 3.0)])
    df["avg_cadence"] = 168.0
    df = with_zones(df, {1: 1200, 2: 2000, 3: 400, 4: 0, 5: 0}, "hr")
    df = with_zones(df, {1: 1200, 2: 2000, 3: 400, 4: 0, 5: 0}, "pwr")
    q = metrics.hr_quality(df)
    assert not q["hr_suspect"].iloc[0]
    assert q["hr_quality"].iloc[0] == "ok"


def test_hr_near_cadence_alone_is_not_enough():
    """Proximity is weak evidence — a genuinely hard run has HR near cadence.

    Without the power cross-check this would condemn every honest hard session.
    """
    df = make_activities([("2026-01-01", "run", 100, 3600, 172.0, 3.0)])
    df["avg_cadence"] = 173.0
    hard = {1: 0, 2: 100, 3: 500, 4: 2500, 5: 500}
    df = with_zones(with_zones(df, hard, "hr"), hard, "pwr")
    assert not metrics.hr_quality(df)["hr_suspect"].iloc[0]


def test_cycling_is_never_cadence_flagged():
    """No foot strike, so the failure mode doesn't apply."""
    df = make_activities([("2026-01-01", "bike", 100, 3600, 90.0, 8.0)])
    df["avg_cadence"] = 90.0
    df = with_zones(df, {1: 0, 2: 100, 3: 500, 4: 2500, 5: 500}, "hr")
    df = with_zones(df, {1: 3000, 2: 600, 3: 0, 4: 0, 5: 0}, "pwr")
    q = metrics.hr_quality(df)
    assert not q["hr_suspect"].iloc[0]
    assert q["hr_quality"].iloc[0] == "ok"


def test_auto_source_uses_power_for_suspect_runs_only():
    suspect = cadence_locked_run("2026-01-01")
    clean = make_activities([("2026-01-02", "bike", 80, 3600, 130.0, 8.0)])
    clean["avg_cadence"] = 85.0
    clean = with_zones(clean, {1: 1800, 2: 1800, 3: 0, 4: 0, 5: 0}, "hr")
    acts = pd.concat([suspect, clean], ignore_index=True)

    z = metrics.zone_time(acts, source="auto")
    assert list(z["zone_source"]) == ["power", "hr"]
    # The suspect run must now read as mostly easy, not mostly hard.
    assert z.loc[0, "low_s"] > z.loc[0, "high_s"]


def test_hr_and_power_sources_disagree_as_expected():
    acts = cadence_locked_run()
    by_hr = metrics.intensity_summary(acts, source="hr")
    by_pwr = metrics.intensity_summary(acts, source="power")
    assert by_hr["shares"]["high"] > 0.5
    assert by_pwr["shares"]["low"] > 0.5
    assert by_pwr["score"] > by_hr["score"]


def test_hr_quality_summary_counts_suspect_runs():
    acts = pd.concat([cadence_locked_run("2026-01-01"),
                      cadence_locked_run("2026-01-02")], ignore_index=True)
    s = metrics.hr_quality_summary(acts)
    assert s["runs"] == 2 and s["suspect"] == 2 and s["share"] == 1.0


def test_unreliable_hr_raises_a_flag():
    acts = pd.concat([cadence_locked_run(f"2026-03-{d:02d}") for d in range(1, 6)],
                     ignore_index=True)
    flags = metrics.training_flags(acts, pd.DataFrame(), pd.DataFrame(),
                                   through=pd.Timestamp("2026-03-06"))
    assert any("heart rate is unreliable" in f["title"] for f in flags)


# ---------------------------------------------------------------------------
# Session tags (RPE / talk test)
# ---------------------------------------------------------------------------

def _tags(rows):
    """Tags keyed the same way make_activities derives its ids."""
    return pd.DataFrame([{"activity_id": int(pd.Timestamp(d).strftime("%Y%m%d")) * 10,
                          "date": pd.Timestamp(d), "rpe": rpe, "feel": feel}
                         for d, rpe, feel in rows])


def test_srpe_load_is_rpe_times_minutes():
    acts = make_activities([("2026-01-01", "run", 100, 3600)])
    out = metrics.apply_tags(acts, _tags([("2026-01-01", 5.0, "moderate")]))
    assert out["srpe_load"].iloc[0] == pytest.approx(5.0 * 60)


def test_apply_tags_without_tags_is_safe():
    acts = make_activities([("2026-01-01", "run", 100)])
    for empty in (None, pd.DataFrame()):
        out = metrics.apply_tags(acts, empty)
        assert out["feel"].isna().all()
        assert out["srpe_load"].isna().all()


def test_feel_tag_overrides_the_sensors():
    """The manual override: you tag it easy, it counts as easy.

    The session below reads as hard on *both* HR and power, so nothing
    automated would rescue it. Only the tag can.
    """
    df = make_activities([("2026-01-01", "run", 100, 3600, 172.0, 3.0)])
    df["avg_cadence"] = 173.0
    hard = {1: 0, 2: 100, 3: 500, 4: 2500, 5: 500}
    acts = with_zones(with_zones(df, hard, "hr"), hard, "pwr")

    untagged = metrics.zone_time(acts, source="auto")
    assert untagged["high_s"].iloc[0] > untagged["low_s"].iloc[0]
    assert untagged["zone_source"].iloc[0] == "hr"

    tagged = metrics.zone_time(acts, source="auto",
                               tags=_tags([("2026-01-01", 2.0, "easy")]))
    assert tagged["zone_source"].iloc[0] == "tagged"
    assert tagged["high_s"].iloc[0] == 0
    assert tagged["low_s"].iloc[0] == pytest.approx(3600, rel=0.01)


def test_feel_tag_preserves_session_duration():
    """An override moves time between bands; it must not invent or lose any."""
    acts = cadence_locked_run()
    for feel in metrics.FEEL_ORDER:
        z = metrics.zone_time(acts, source="auto",
                              tags=_tags([("2026-01-01", 5.0, feel)]))
        assert z["zone_total_s"].iloc[0] == pytest.approx(3600, rel=0.01)


def test_hard_tag_keeps_a_warmup_allowance():
    """No session is hard from the first step — a 'hard' tag isn't 100% Z4-5."""
    z = metrics.zone_time(cadence_locked_run(), source="auto",
                          tags=_tags([("2026-01-01", 8.0, "hard")]))
    assert z["high_s"].iloc[0] > z["low_s"].iloc[0]
    assert z["low_s"].iloc[0] > 0


def test_tag_lifts_the_intensity_distribution():
    """Tagging hard-looking runs as easy moves the whole distribution."""
    acts = pd.concat([cadence_locked_run(f"2026-03-{d:02d}") for d in range(1, 5)],
                     ignore_index=True)
    # Strip power so nothing automated can rescue these.
    acts = acts.drop(columns=[c for c in acts.columns if c.startswith("pwr_z")])
    before = metrics.intensity_summary(acts, source="auto")
    after = metrics.intensity_summary(
        acts, source="auto",
        tags=_tags([(f"2026-03-{d:02d}", 3.0, "easy") for d in range(1, 5)]))
    assert before["shares"]["high"] > 0.5
    assert after["shares"]["low"] == pytest.approx(1.0)
    assert after["score"] > before["score"]


def test_tagged_runs_stop_counting_as_unresolved():
    acts = pd.concat([cadence_locked_run(f"2026-03-{d:02d}") for d in range(1, 5)],
                     ignore_index=True)
    tags = _tags([(f"2026-03-{d:02d}", 3.0, "easy") for d in range(1, 3)])
    s = metrics.hr_quality_summary(acts, tags=tags)
    assert s["runs"] == 4 and s["tagged"] == 2 and s["unresolved"] == 2


def test_untagged_sessions_are_untouched_by_the_override():
    tagged = cadence_locked_run("2026-01-01")
    other = cadence_locked_run("2026-01-02")
    acts = pd.concat([tagged, other], ignore_index=True)
    z = metrics.zone_time(acts, source="auto",
                          tags=_tags([("2026-01-01", 3.0, "easy")]))
    assert list(z["zone_source"]) == ["tagged", "power"]


def test_feel_confirms_power_over_hr():
    """The athlete says easy; power agrees; HR does not.

    This is the whole point of tagging — an independent read that settles which
    sensor to believe.
    """
    acts = cadence_locked_run("2026-01-01")
    tags = _tags([("2026-01-01", 3.0, "easy")])
    assert metrics.feel_vs_zones(acts, tags, source="auto")["agrees"].iloc[0]
    assert not metrics.feel_vs_zones(acts, tags, source="hr")["agrees"].iloc[0]


def test_tag_coverage():
    acts = make_activities([("2026-01-01", "run", 100), ("2026-01-02", "run", 100)])
    cov = metrics.tag_coverage(acts, _tags([("2026-01-01", 3.0, "easy")]), "run")
    assert cov == {"total": 2, "tagged": 1, "share": 0.5}


# ---------------------------------------------------------------------------
# Run efficiency
# ---------------------------------------------------------------------------

def test_efficiency_drops_runs_with_untrustworthy_hr():
    """Metres per heartbeat is meaningless when the heartbeat is cadence."""
    assert metrics.run_efficiency(cadence_locked_run()).empty

def test_aerobic_filter_excludes_hard_and_short_runs():
    hard = with_zones(make_activities([("2026-01-01", "run", 100, 3600)]),
                      {1: 0, 2: 100, 3: 500, 4: 2500, 5: 500})
    easy = with_zones(make_activities([("2026-01-02", "run", 60, 3600)]),
                      {1: 1800, 2: 1500, 3: 300, 4: 0, 5: 0})
    short = with_zones(make_activities([("2026-01-03", "run", 20, 600)]),
                       {1: 300, 2: 300, 3: 0, 4: 0, 5: 0})
    acts = pd.concat([hard, easy, short], ignore_index=True)
    out = metrics.run_efficiency(acts, aerobic_only=True)
    assert len(out) == 1
    assert out["date"].iloc[0] == pd.Timestamp("2026-01-02")


def test_efficiency_factor_is_metres_per_beat():
    acts = make_activities([("2026-01-01", "run", 60, 3600, 150.0, 3.0)])
    assert metrics.run_efficiency(acts)["efficiency"].iloc[0] == \
        pytest.approx(3.0 * 60 / 150)


# ---------------------------------------------------------------------------
# HRV baseline
# ---------------------------------------------------------------------------

def _daily_hrv(values, start="2026-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.DataFrame({"date": idx, "hrv_overnight": values})


def test_hrv_stable_series_reads_mostly_normal():
    """A steady series should sit inside its own band most of the time.

    Asserted over the distribution rather than the final day: with a +/-0.5 SD
    band, individual days crossing the edge is the expected behaviour, not a
    fault.
    """
    rng = np.random.default_rng(0)
    daily = _daily_hrv(60 + rng.normal(0, 2, 120))
    hb = metrics.hrv_baseline(daily).dropna(subset=["z"])
    assert not hb.empty
    assert (hb["status"] == "normal").mean() > 0.6
    assert hb["z"].abs().max() < 3


def test_hrv_drop_reads_suppressed():
    rng = np.random.default_rng(1)
    values = np.concatenate([60 + rng.normal(0, 2, 100),
                             42 + rng.normal(0, 2, 10)])
    hb = metrics.hrv_baseline(_daily_hrv(values)).dropna(subset=["z"])
    assert hb["status"].iloc[-1] == "suppressed"
    assert hb["z"].iloc[-1] < -1


def test_hrv_baseline_handles_missing_column():
    assert metrics.hrv_baseline(pd.DataFrame({"date": []})).empty


def test_hrv_cv_is_a_positive_percentage_for_a_varying_series():
    rng = np.random.default_rng(3)
    hb = metrics.hrv_baseline(_daily_hrv(60 + rng.normal(0, 4, 120)))
    cv = hb["cv"].dropna()
    assert not cv.empty
    assert (cv > 0).all()
    # A ~4/60 dispersion is a few percent, nowhere near a collapse.
    assert not hb["cv_collapsed"].iloc[-1]


def test_hrv_cv_collapse_is_flagged_when_variability_goes_flat():
    """A suppressed mean whose day-to-day variation flatlines is the collapse."""
    rng = np.random.default_rng(4)
    values = np.concatenate([60 + rng.normal(0, 4, 100),   # normal variability
                             45 + rng.normal(0, 0.1, 14)])  # low and near-constant
    hb = metrics.hrv_baseline(_daily_hrv(values))
    assert hb["status"].iloc[-1] == "suppressed"
    assert hb["cv_collapsed"].iloc[-1]


def test_hrv_collapse_raises_the_overreaching_flag_above_a_plain_suppression():
    rng = np.random.default_rng(5)
    values = np.concatenate([60 + rng.normal(0, 4, 100),
                             45 + rng.normal(0, 0.1, 14)])
    daily = _daily_hrv(values)
    flags = metrics.training_flags(
        pd.DataFrame(), daily, pd.DataFrame(), through=daily["date"].max())
    titles = [f["title"] for f in flags]
    assert any("variability collapsing" in t for t in titles)
    nfor = next(f for f in flags if "variability collapsing" in f["title"])
    assert nfor["status"] == "critical"


# ---------------------------------------------------------------------------
# Monotony
# ---------------------------------------------------------------------------

def test_monotony_undefined_below_three_training_days():
    acts = make_activities([("2026-01-01", "run", 100), ("2026-01-05", "run", 100)])
    ms = metrics.monotony_strain(acts, through=pd.Timestamp("2026-01-20"))
    assert ms["monotony"].isna().all()


def test_identical_daily_load_is_maximally_monotonous():
    """Zero variance is the worst case, not an unknown one."""
    acts = make_activities([(f"2026-01-{d:02d}", "run", 50) for d in range(1, 21)])
    ms = metrics.monotony_strain(acts, through=pd.Timestamp("2026-01-20"))
    assert ms["monotony"].iloc[-1] == pytest.approx(metrics.MONOTONY_CAP)


def test_alternating_load_is_less_monotonous_than_flat_load():
    flat = make_activities([(f"2026-01-{d:02d}", "run", 50) for d in range(1, 21)])
    varied = make_activities([(f"2026-01-{d:02d}", "run", 90 if d % 2 else 20)
                              for d in range(1, 21)])
    end = pd.Timestamp("2026-01-20")
    assert (metrics.monotony_strain(varied, through=end)["monotony"].iloc[-1] <
            metrics.monotony_strain(flat, through=end)["monotony"].iloc[-1])


# ---------------------------------------------------------------------------
# Discipline balance
# ---------------------------------------------------------------------------

def test_discipline_balance_flags_neglected_swim():
    acts = make_activities(
        [(f"2026-03-{d:02d}", "bike", 80) for d in range(1, 28)] +
        [("2026-01-05", "swim", 40)])
    bal = metrics.discipline_balance(acts, through=pd.Timestamp("2026-03-28"))
    swim = bal.set_index("sport").loc["swim"]
    assert swim["stale"]
    assert swim["days_since"] > 7
    bike = bal.set_index("sport").loc["bike"]
    assert bike["gap_pct"] > 0  # over-represented vs the 50% target


def test_discipline_shares_sum_to_one_over_covered_sports():
    acts = make_activities([("2026-03-01", "swim", 30), ("2026-03-02", "bike", 90),
                            ("2026-03-03", "run", 60)])
    bal = metrics.discipline_balance(acts, through=pd.Timestamp("2026-03-04"))
    assert bal["share"].sum() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Progression score
# ---------------------------------------------------------------------------

def _blank_daily(n=120, start="2026-01-01"):
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"date": idx, "resting_hr": np.nan,
                         "hrv_overnight": np.nan})


def test_pillar_is_none_rather_than_collapsing_to_one_input():
    """Recovery must not report a great score off TSB alone.

    Without this, stopping training raises TSB, which raises Recovery, which
    reports "recovery is strong" to someone who has simply stopped.
    """
    acts = make_activities([(f"2026-01-{d:02d}", "run", 100) for d in range(1, 29)])
    prog = metrics.progression_score(
        acts, _blank_daily(), pd.DataFrame({"date": [], "sleep_score": []}),
        through=pd.Timestamp("2026-03-01"))
    assert prog is not None
    assert prog["pillars"]["recovery"] is None


def test_score_falls_during_a_layoff():
    acts = make_activities([(f"2026-{m:02d}-{d:02d}", "run", 90)
                            for m in (1, 2, 3) for d in range(1, 28)])
    training = metrics.progression_score(acts, _blank_daily(), pd.DataFrame(),
                                         through=pd.Timestamp("2026-03-27"))
    laid_off = metrics.progression_score(acts, _blank_daily(), pd.DataFrame(),
                                         through=pd.Timestamp("2026-05-01"))
    assert laid_off["score"] < training["score"]


def test_progression_series_has_all_pillars():
    acts = make_activities([(f"2026-01-{d:02d}", "run", 100) for d in range(1, 29)])
    s = metrics.progression_series(acts, _blank_daily(), pd.DataFrame(),
                                   through=pd.Timestamp("2026-03-01"))
    for p in metrics._PILLARS:
        assert p in s.columns


def test_unknown_philosophy_falls_back_to_balanced():
    acts = make_activities([(f"2026-01-{d:02d}", "run", 100) for d in range(1, 29)])
    kw = {"daily": _blank_daily(), "sleep": pd.DataFrame(),
          "through": pd.Timestamp("2026-03-01")}
    a = metrics.progression_series(acts, philosophy="nonsense", **kw)
    b = metrics.progression_series(acts, philosophy="balanced", **kw)
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------------------
# Race projection
# ---------------------------------------------------------------------------

def test_race_projection_tapers_into_positive_form():
    acts = make_activities([(f"2026-{m:02d}-{d:02d}", "bike", 90)
                            for m in (1, 2, 3) for d in range(1, 28)])
    proj = metrics.race_projection(acts, "2026-05-01",
                                   through=pd.Timestamp("2026-03-27"))
    assert proj["tsb_race"] > 0
    assert proj["taper_start"] == pd.Timestamp("2026-04-17")
    assert len(proj["path"]) == proj["days_out"]


def test_race_projection_none_for_a_past_race():
    acts = make_activities([("2026-01-01", "run", 100)])
    assert metrics.race_projection(acts, "2025-01-01",
                                   through=pd.Timestamp("2026-01-02")) is None


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def test_stale_data_flag_ranks_first():
    acts = make_activities([("2026-01-01", "run", 100)])
    flags = metrics.training_flags(acts, pd.DataFrame(), pd.DataFrame(),
                                   through=pd.Timestamp("2026-03-01"))
    assert flags[0]["status"] == "critical"
    assert "days old" in flags[0]["title"]


def test_no_stale_flag_when_data_is_current():
    acts = make_activities([("2026-03-01", "run", 100)])
    flags = metrics.training_flags(acts, pd.DataFrame(), pd.DataFrame(),
                                   through=pd.Timestamp("2026-03-02"))
    assert not any("days old" in f["title"] for f in flags)


# ---------------------------------------------------------------------------
# summary_stats
# ---------------------------------------------------------------------------

def test_summary_stats_surfaces_both_run_and_bike_vo2max():
    acts = make_activities([("2026-01-01", "run", 100), ("2026-01-02", "bike", 100)])
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
        "resting_hr": [48.0, 47.0],
        "vo2max_run": [54.0, 55.0],
        "vo2max_bike": [58.0, 59.0],
    })
    stats = metrics.summary_stats(acts, daily)
    assert stats["vo2max_run"] == 55.0
    assert stats["vo2max_bike"] == 59.0


def test_summary_stats_bike_vo2max_is_none_when_absent():
    acts = make_activities([("2026-01-01", "run", 100)])
    daily = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]),
                          "vo2max_run": [54.0]})
    assert metrics.summary_stats(acts, daily)["vo2max_bike"] is None
