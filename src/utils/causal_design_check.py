"""
Pre-flight design checks for "did X cause Y" analyses on player-level panel
data, built from three concrete rejected designs (see
memory/feedback_causal_design_pitfalls.md, 2026-09-07 Brand_2.lv Engage
analysis).

This is deliberately NOT an estimator -- it doesn't implement DiD or
propensity matching. The actual lesson from that analysis was that the
standard causal toolkit is usually the wrong tool for this project's data:
there's rarely a real experiment (whole-brand simultaneous exposure, no
random assignment) and the player population keeps shifting composition
underneath any snapshot. Building a matching/DiD estimator without that
context would risk producing confident numbers from a design shape this
project doesn't actually have.

Instead, this checks a PROPOSED design against the three specific failure
modes already paid for once, before a causal claim gets reported:

  1. Frozen-snapshot grouping: a group label assigned once (at one anchor
     date) and tracked forward as fixed mechanically produces
     regression-to-the-mean, independent of any real intervention.
  2. Proxy treatment/control: a split that doesn't correspond to actual
     differential exposure to the intervention isn't a real treatment/
     control division, no matter how the two groups otherwise differ.
  3. Placebo/negative-control test: the concrete tell that actually caught
     the flawed Brand_2 design was running the identical computation on
     brands with NO intervention and seeing the same-magnitude "effect."
     That was noticed by eye, across separate write-ups, after the fact --
     this automates it as a check that runs before a claim is made.

Usage: call check_causal_design() before reporting a causal claim from a
before/after or treatment/control comparison on this project's data.
"""
import pandas as pd


def _diff_in_diff(df, domain_col, domain, date_col, treatment_date, group_col, metric_col):
    d = df[df[domain_col] == domain]
    dates = pd.to_datetime(d[date_col])
    pre = d[dates < pd.to_datetime(treatment_date)]
    post = d[dates >= pd.to_datetime(treatment_date)]
    groups = sorted(d[group_col].dropna().unique())
    if len(groups) != 2 or pre.empty or post.empty:
        return None
    g_a, g_b = groups
    pre_a = pre.loc[pre[group_col] == g_a, metric_col].mean()
    pre_b = pre.loc[pre[group_col] == g_b, metric_col].mean()
    post_a = post.loc[post[group_col] == g_a, metric_col].mean()
    post_b = post.loc[post[group_col] == g_b, metric_col].mean()
    if any(pd.isna(v) for v in (pre_a, pre_b, post_a, post_b)):
        return None
    return (post_a - pre_a) - (post_b - pre_b)


def check_causal_design(df, group_col, metric_col, date_col, domain_col, player_col,
                         treatment_domain, treatment_date, control_domains,
                         exposure_reason, frozen_label_threshold=0.95, placebo_tolerance=0.5):
    """
    df: player-level panel, one row per (player, period), containing
        domain_col, player_col, date_col, group_col (the proposed treatment/
        control split), metric_col (the outcome being tested).
    treatment_domain: the brand where the intervention actually happened.
    treatment_date: the date the intervention took effect.
    control_domains: brands with NO intervention, used as a placebo test --
        the identical group_col split and DiD computation is run on each of
        these; if the resulting "effect" is not meaningfully smaller than
        the treatment domain's, that's the exact tell from the rejected
        Brand_2 designs (see module docstring, lesson 3).
    exposure_reason: a required, analyst-written one-sentence statement of
        why group_col actually corresponds to differential exposure to the
        treatment. Not validated for content (that's a human judgment call)
        -- only checked for being non-empty, because writing this sentence
        is itself what exposes a proxy split (see lesson 2).
    frozen_label_threshold: if this fraction (or more) of players have a
        single, unchanging group_col value across the whole window, warn
        that the grouping looks like a frozen snapshot (lesson 1).
    placebo_tolerance: a control domain's |DiD effect| >= this fraction of
        the treatment domain's |DiD effect|, with the same sign, is flagged
        as a placebo failure (lesson 3).

    Returns {'warnings': [...], 'placebo_results': DataFrame,
             'treatment_effect': float or None, 'ok': bool}.
    Prints a human-readable summary either way.
    """
    warnings = []

    # --- Lesson 2: proxy treatment/control ---
    if not exposure_reason or not exposure_reason.strip():
        warnings.append(
            f"No exposure_reason given. State in one sentence why the '{group_col}' split "
            "corresponds to who was actually exposed to the treatment, not just who differs on "
            "some other measure. If you can't state that sentence, this split doesn't isolate the "
            "treatment (feedback_causal_design_pitfalls.md, lesson 2)."
        )
    if treatment_domain in control_domains:
        warnings.append(f"treatment_domain '{treatment_domain}' also listed in control_domains -- remove it.")

    # --- Lesson 1: frozen-snapshot grouping ---
    per_player_label_counts = df.groupby(player_col)[group_col].nunique()
    frozen_share = (per_player_label_counts == 1).mean() if len(per_player_label_counts) else 0.0
    if frozen_share >= frozen_label_threshold:
        warnings.append(
            f"{frozen_share:.0%} of players have a single, unchanging '{group_col}' value across the "
            "whole window -- this looks like a frozen-snapshot label assigned once and tracked forward, "
            "which mechanically produces regression-to-the-mean regardless of any real intervention "
            "(feedback_causal_design_pitfalls.md, lesson 1 and 3)."
        )

    # --- Lesson 3, mechanized: placebo test on control domains ---
    treatment_effect = _diff_in_diff(df, domain_col, treatment_domain, date_col, treatment_date, group_col, metric_col)
    if treatment_effect is None:
        warnings.append(
            f"Could not compute a DiD effect for treatment_domain '{treatment_domain}' -- check "
            f"group_col has exactly 2 values present, and both pre/post windows have data."
        )

    placebo_rows = []
    for cd in control_domains:
        cd_effect = _diff_in_diff(df, domain_col, cd, date_col, treatment_date, group_col, metric_col)
        placebo_rows.append({'domain': cd, 'did_effect': cd_effect})
        if cd_effect is not None and treatment_effect is not None and treatment_effect != 0:
            ratio = abs(cd_effect) / abs(treatment_effect)
            same_sign = (cd_effect > 0) == (treatment_effect > 0)
            if ratio >= placebo_tolerance and same_sign:
                warnings.append(
                    f"Placebo check FAILED: control domain '{cd}' (no intervention) shows a same-sign "
                    f"effect {ratio:.0%} the size of the treatment domain's ({cd_effect:+.4f} vs "
                    f"{treatment_effect:+.4f}) -- this is the exact tell that caught the rejected Brand_2 "
                    "Engage designs: the DESIGN, not the intervention, is producing the result "
                    "(feedback_causal_design_pitfalls.md, lesson 3)."
                )

    placebo_df = pd.DataFrame(placebo_rows)
    ok = len(warnings) == 0

    print("Causal design check:", "PASSED -- no known failure mode detected." if ok else f"{len(warnings)} issue(s) found:")
    for w in warnings:
        print(f"  - {w}")
    if treatment_effect is not None:
        print(f"  Treatment domain ({treatment_domain}) DiD effect: {treatment_effect:+.4f}")
    if not placebo_df.empty:
        print(placebo_df.to_string(index=False))

    return {'warnings': warnings, 'placebo_results': placebo_df, 'treatment_effect': treatment_effect, 'ok': ok}
