# Outlier & Margin Variance Standard

**Scope:** Casino, Live Casino, Sportsbook — for the brands in the bi-weekly pack only. Twelve domains:

| Market | Domains |
|---|---|
| Latvia | `brand_1.lv`, `brand_3.lv`, `brand_2.lv` |
| Estonia | `brand_1.ee`, `brand_4.ee`, `brand_6_estonia`, `brand_7_estonia` |
| Lithuania | `brand_1.lt`, `brand_5.lt` |
| Finland | `brand_6_finland`, `brand_7_finland` |
| Sweden | `brand_6se_sweden` (passive — no new investment) |

**Explicitly out of scope**, even though they appear in `monthly_player_data.csv`: `brand_8_estonia`, `brand_8_finland`, `brand_8_malta`, `brand_9.bg`, `brand_10.net`. Malta in particular sits outside the NCE remit. The last two carry zero turnover throughout and should be resolved at source.

`brand_1.com` is closed — YTD only, excluded from all forward analysis and from all baseline and threshold estimation. It is **retained in `config/hv_segmentation.yaml` as an explicit skip** (`skip: true`, `frozen_bar: null`) rather than deleted, so the exclusion is recorded rather than inferred from absence. Config therefore holds 13 domain blocks for 12 in-scope brands.

**Status:** v2.0 — Layer 1 built and validated against 19 months of data. Layer 2 not yet built; its parameters are still estimates.
**Applies to:** bi-weekly brand performance analysis, weekly/monthly/YTD views.

**Does not apply to:** bonus abuse detection, fraud, or risk/integrity reporting. This standard exists to stop noise being misread as trend in commercial reporting. Abuse detection is an adversarial problem with different signals, a different unit of analysis and a different cost of error — see Section 10. The only link between the two is the handoff at 2.7(3).

---

## 0. Status and adoption path

| Stage | Work | State |
|---|---|---|
| **1** | Layer 1 — HV segmentation, frozen thresholds, four-term decomposition | **Built and validated.** `src/processing/hv_segmentation.py`, `config/hv_segmentation.yaml` |
| **2** | Layer 2 — margin variance against trailing baseline | **Not built.** Section 3. Runs off `brand_daily_kpis_comparison.csv`, touches nothing existing |
| **3** | Fix the pooled 13-week IQR in `weekly_orchestrator.py` | **Open defect.** See 9.1 — affects live packs |
| **4** | Retire `get_outliers_iqr()`; one outlier definition instead of two | Open |
| **5** | Add a product dimension to the weekly schema | Open. Data-model change, largest single item |

**Minimum useful read:** Section 1 (why behaviour and luck are separated), Section 2.2–2.4 (the high-value definition and its sample gate), Sections 3.1–3.2 (the margin baseline), Section 9.1 (what is currently broken in production).

**Do not reimplement anything in the 2.1 rejection table.** Those five rules were built and disproved against real data. The table exists to stop the work being repeated.

---

## 1. Core principle

Two different things get called "outliers" in casino reporting, and they need opposite treatment:

| | Nature | Unit | Treatment |
|---|---|---|---|
| **Behavioural** | Who the players were and what they staked | Player | Segment and attribute (Layer 1). Do **not** flag — see 2.1 |
| **Stochastic (luck)** | Volume was normal, the outcome wasn't | Brand-product-period (Casino) / event (Sportsbook) | Do **not** trim. Report as variance against baseline (Layer 2) |

Applied to the standing causal decomposition:

- **Casino / Live Casino = Actives × ATPU × Margin** → Actives and ATPU split into high-value and regular tiers (Layer 1). Margin gets a variance band (Layer 2).
- **Sportsbook = Turnover vs Margin** → Turnover splits by tier (Layer 1). Margin is Layer 2 plus event-level exposure review (Section 6).

Never attribute a movement to "player behaviour" when the turnover was flat and only the margin moved. State which layer the movement came from.

---

## 2. Layer 1 — High-value segmentation and attribution (player level)

Run on **turnover** only. Not GGR, not NGR. Outcome metrics belong in Layer 2.

### 2.1 Layer 1 does not detect anomalies — it attributes movement

This is the conclusion of a full backtest, not a design preference. Five detection rules were built and tested against 19 months of player-level data across four brands spanning a 26× range of base size. All five failed, and they failed for one shared reason: **in a player base where the median player is 8–33× below the mean, the largest players materially move the total every period.** Concentration is the normal state, so a rule that detects concentration fires constantly and discriminates nothing.

The decisive evidence is the correlation test. Across 11 brand-products × 5 concentration metrics (Gini, HHI, skewness, kurtosis, top-1% share), none predicts period-on-period turnover movement — and the null result is strongest on `brand_1.lv`, the largest and best-sampled brand, where HHI correlations against turnover change are −0.003, 0.194 and 0.174 across the three products. Positive results appear only where samples are too thin to trust.

Rules tested and rejected — **do not reimplement these:**

| Rule | Why it fails |
|---|---|
| 3×IQR on player turnover | Firing rate is fixed by distribution skewness, not by events. Flagged 7.4–15.3% of actives every single month, 57/57 product-months |
| P90 of trailing top-player-share | P90 is not estimable at n≤12, and a declining base makes the history non-stationary. Fired 5.6% on one product, 33% on another |
| Share-of-turnover tier thresholds | 7% of a 205-active product is ~14× the mean player; the same 7% on 3,000 actives is ~210×. Same number, different meaning. Fired 100% of months on thin products |
| Per-player 2pp delta-impact | Fired 94% of months. On `brand_6_estonia` live casino, 14 of the top 20 players individually cleared 2pp in one month |

### 2.2 High-value classification — frozen threshold

The useful question is not "is this player unusual" but "is the high-value cohort shrinking faster than the base". That requires a definition that **holds still**, so movement in the cohort is real movement rather than the boundary shifting.

**Frozen euro threshold, per brand-product.** Computed once from calendar 2025 and stored in `config/hv_segmentation.yaml`. Never recalculated at runtime.

```
frozen_bar    = P99 of pooled player trailing-3-month turnover, calendar 2025
monthly_rate  = frozen_bar × 0.5
```

A player is high-value in month M if **either**:

- **(a)** trailing-3-month turnover over [M−3, M−2, M−1] exceeds `frozen_bar`, **or**
- **(b)** month-M turnover alone exceeds `monthly_rate`

Path (a) is the primary test. Path (b) exists because a new whale has no trailing history and would otherwise be classified as regular — which happened on `brand_6_estonia` live casino in April 2025, where a player doing ~€3.6m sat in the regular bucket and dragged regular ATPU to €14,085.

Report (a) and (b) counts separately. Validated across all 26 kept brand-products: (a) totals exceed (b) by 1.4–3.4× everywhere, so the cohort is trailing-driven as intended. Because a higher fraction makes (b) harder to clear, this dominance holds at 1/3 as well — the 0.5 choice is not load-bearing. If (b) ever comes to exceed (a) for a brand-product, that cohort is being driven by single-month qualification and is less stable than it looks.

**Why the threshold must be absolute, not a percentile.** Under a "top 1%" rule, high-value count falls in exact proportion to the base by construction, so the question "are we losing high-value players disproportionately" becomes unanswerable. Only a fixed monetary bar lets the count move independently.

**Why 3 months and not 1 or 6.** One month is dominated by variance — a single session or holiday moves a player across the line. Six months makes a departure invisible for half a year. Three is the shortest window that smooths without hiding churn.

**Tag behaviour.** The window slides every month; the bar does not. A player who stops playing drops out of the cohort as their trailing turnover decays, with roughly a two-month lag. This means **HV turnover falls immediately on a departure while HV count falls later** — report both, because count alone understates a recent exodus.

### 2.3 The four-term decomposition

This is the deliverable Layer 1 exists to produce:

```
turnover = (HV_count × HV_ATPU) + (Reg_count × Reg_ATPU)

turnover_delta = HV_count_effect  + HV_ATPU_effect
               + Reg_count_effect + Reg_ATPU_effect
```

Midpoint (Bennet) decomposition, which closes to zero residual. Implemented in `src/processing/hv_segmentation.py` as `decompose_turnover_delta()`.

Read it as a single sentence in the pack: *"Casino turnover −12%: high-value count −18% contributing −9pp, regular count −4% contributing −2pp, ATPU broadly flat in both tiers."* That answers whether a decline is a top-end retention problem or a base-volume problem, which no flag ever did.

It slots into the standing `Actives × ATPU × Margin` breakdown as a split of the first two terms. It does not replace it.

### 2.4 Sample gate — effective player count

**Skip any brand-product where 1/HHI < 30.** HHI is the sum of squared player turnover shares; 1/HHI is the number of equal-sized players that would produce the observed concentration.

This is a derived gate, not a guessed one, and it excludes 13 of 40 brand-products. `brand_5.lt` live casino has an effective N of **1.8** — statistically two people. `brand_6_estonia` live casino is 6.6. A "cohort" there is a fiction.

Skipped brand-products are reported by **attribution only**: name the top players' contribution to the delta and do not imply a tiered structure exists. They are not dropped from the pack.

A brand-product with zero turnover in the source data raises rather than skips — an empty product is a data question, not a concentration one.

**Secondary use:** a sustained fall in 1/HHI means revenue is consolidating into fewer players. That is a concentration risk worth naming in its own right, regardless of whether turnover is up or down.

### 2.5 Gini as a structural tripwire

Gini on player turnover is exceptionally stable — `brand_1.lv` digital casino runs 0.8271 ± 0.0046 across 19 months, a CV of 0.6%. Its standard deviation scales as roughly 1/√n, so the expected SD is derivable from base size rather than estimated per brand.

Use: not for flagging players. If Gini moves more than 3 SD, the **player mix** changed — game portfolio, acquisition channel, or a pipeline fault. It is a structural-break detector, and the pipeline fault case is the most common.

### 2.6 Event months

Major ice hockey tournaments drive a large influx of low-value sportsbook players in Latvia. Left unflagged, an event month reads as "regular base grew 100%, ATPU fell 40%" — arithmetically true, analytically misleading.

| Brand-product | Event months |
|---|---|
| `brand_1.lv` / sb | 2025-05, 2026-02, 2026-05 |
| `brand_3.lv` / sb | 2025-05, 2026-02, 2026-05 |
| `brand_2.lv` / sb | 2026-02, 2026-05 (no sportsbook in 2025) |

Stored as `event_months` in `config/hv_segmentation.yaml` so the list is explicit and editable. Two distinct uses:

- **Threshold computation:** event months are *excluded* when computing the frozen bar, because the low-value influx drags the 99th percentile down. Confirmed on both Latvian sportsbook brands — `brand_1.lv` moved from €29,369 to €32,784, `brand_3.lv` from €20,097 to €21,028. Both moved up, as the bias direction predicts.
- **Reporting:** event months are *flagged, never excluded*. The revenue is real. The flag makes the regular-tier swing legible instead of reading as a trend.

**Open question, not an assumption.** `CLAUDE.md` holds that sportsbook event spikes correlate with casino cross-sell lift. The influx is now known to be structurally low-value, so the question is whether these players convert to casino at all. The data does not obviously show it: `brand_1.lv` digital casino regular count was 23,436 in June 2025 against 24,199 in May, and 22,598 in March 2026 against 22,323 in February — no visible lift either time. Worth testing properly before the cross-sell rule is relied on.

### 2.7 Disposition of an attribution entry

Every named player in the attribution list gets one of four labels, recorded in the meeting notes:

1. **Genuine VIP** — expected, disclose impact, no adjustment to the trend read
2. **New VIP acquisition** — a commercial event, name it
3. **Suspected abuse / bot / fraud** — escalate outside the performance meeting (see Section 10)
4. **Data error** — log to `correction_log.md`, exclude, re-run

---

## 3. Layer 2 — Margin variance against baseline

No theoretical margins are available in the data, so the baseline is empirical.

### 3.1 Baseline construction

**Trailing 12 months, turnover-weighted, per brand × product × market, lagged one period.**

```
M_base = Σ GGR (12m window) / Σ Turnover (12m window)
```

Four things that matter more than the window length:

- **Turnover-weight it.** Take total GGR over total turnover. Do *not* average the weekly margin percentages — that gives a 40k-turnover week the same weight as a 400k week and is the single most common error in this calculation.
- **Lag it.** The baseline for period *t* is built from *t−1* backwards. A period must never be part of its own comparator.
- **Freeze it quarterly.** Recompute on a fixed quarterly cadence, not when a number looks wrong. Historical comparisons must not shift retroactively between meetings.
- **Estimate per brand, not per market.** Game mix, provider mix and bonus policy differ enough between brands that a market-level margin is not a valid comparator for any individual brand in it.

### 3.2 Why 12 months and not 6

- **Sportsbook: 12 months is mandatory, not preferred.** Sportsbook margin is driven by bet mix (singles vs. accumulators, pre-match vs. live), and bet mix is strongly seasonal. Any 6-month window is either football-heavy or summer-heavy, so the baseline is systematically biased in a known direction and the bias flips every two quarters.
- **Live Casino: 12 months for stability.** Hold sits low (roughly 0.5–2%), so absolute swings that look trivial are large in relative terms. A 0.3pp move is noise on slots and a 20–30% relative shift on live tables. Live is also the most VIP-concentrated product, meaning a 6-month window can effectively be measuring the margin achieved against two or three specific players.
- **Casino / slots: 6 months would be statistically sufficient**, since margin converges fast on high bet volumes. Use 12 months anyway for operational uniformity — one window across all three products, one thing to remember.

### 3.3 The 6-month window still has a job: drift detection

Compute a 6-month baseline alongside the 12-month one. It is not used as a comparator. It is used to detect that the 12-month baseline has gone stale:

```
drift = M_base(6m) − M_base(12m)
```

If `|drift|` exceeds **15% of M_base(12m)** for Casino or Live Casino, or **1.5pp** for Sportsbook, the product's economics have changed. Structural changes are staleness, not luck. Likely causes to check, in order:

1. Game or provider mix shift (new provider live, a title cannibalising volume)
2. Bonus policy or bonus volume change
3. Live table limit or table portfolio change
4. Sportsbook: shift in live betting share, or a change in margin/pricing settings
5. Tax or regulatory change affecting reported GGR

On a confirmed structural change: switch that brand-product to the 6-month baseline, log the switch and the reason in `correction_log.md`, and revert to 12-month once 12 months of post-change data exist.

### 3.4 Variance band

Empirical, not theoretical — no distributional assumption to defend:

- Take the weekly (or monthly, matching the reporting period) margin observations in the 12-month window.
- Band = **P10 to P90** of those observations.
- Report the band alongside the actual, and state where in it the period falls.

Inside the band → the period is normal. Say so explicitly rather than manufacturing a narrative. Outside the band → name it as a margin event, quantify it, and do not blend it into a behavioural explanation.

Volume adjustment: variance shrinks with turnover, so a low-turnover brand-week deserves a wider band. Where turnover is below 50% of the window median, widen the band by `√(median_turnover / actual_turnover)` before reading it.

### 3.5 Minimum sample gates and fallbacks

Two gates, both required:

- **Effective player count: 1/HHI ≥ 30**, the same gate as Layer 1 (Section 2.4). Below this the margin is a handful of players' luck rather than a product's economics — `brand_5.lt` live casino has an effective N of 1.8, so its "margin" is one person's results.
- **≥ 26 periods** with non-zero turnover in the window, **and** window turnover **≥ 20×** the median single-period turnover.

The 1/HHI gate is derived from measurement; the period counts are still estimates and should be checked on the first Layer 2 run.

If any gate fails, fall back in this order:

1. Same product, same brand, market cluster average (Baltic cluster: LV/EE/LT; Nordic cluster: FI/SE)
2. Same product, cross-brand pooled within cluster
3. No baseline — report the period without a margin comparison and say the sample is insufficient. This is a legitimate output.

Exclude `brand_1.com` from all baseline estimation. It closed in 2026 and is not analysed forward; its history should not shape live brands' comparators.

**Event months are excluded from baseline estimation and flagged in reporting**, per Section 2.6. The mechanism is the same as for the frozen thresholds: an influx of low-value players shifts the distribution the baseline is meant to describe.

### 3.6 Bonus contamination — must be handled before cross-brand comparison

Bonus turnover carries different effective economics than cash turnover, and Lithuania brands never receive bonuses at all. Comparing raw margin between a Lithuanian brand and a bonus-active brand is therefore not a like-for-like comparison.

Where the data supports it, compute margin on **cash turnover only** and hold bonus cost as a separate line. Where it does not, restrict margin comparisons to within-brand time series and flag any cross-brand margin comparison as non-comparable in the output.

**Open item:** confirm whether bonus/cash turnover split is available at product level in the current data model. Until confirmed, cross-brand margin comparisons are not presented.

---

## 4. Product-specific parameters

| | Casino (slots) | Live Casino | Sportsbook |
|---|---|---|---|
| Baseline window | 12m turnover-weighted | 12m turnover-weighted | 12m turnover-weighted |
| Typical hold, orientation only | ~3–5% | ~0.5–2% | mid-to-high single digits, high dispersion |
| Margin stability | High | Moderate, VIP-sensitive | Low |
| Layer 1 unit | Player tier | Player tier | **Event / selection** + player tier |
| Drift trigger | 15% relative | 15% relative | 1.5pp absolute |
| Main contamination risk | Game mix, jackpots | VIP concentration, table limits | Bet mix, seasonality, free bets, LV hockey events |

The hold figures are orientation for sanity-checking a computed baseline, not targets. Each brand's own computed baseline governs.

Bingo, poker and other minor verticals are out of scope. If any grows past ~5% of brand NGR, it needs its own section before it enters the analysis.

---

## 5. Jackpots

Progressive jackpot hits are **not** outliers and are never winsorized. They are a known cost with a known frequency.

- Identify jackpot payouts separately in the period.
- Report GGR both with and without the hit, labelled.
- Do not let a jackpot week enter the baseline uncorrected — either exclude the payout from baseline GGR or exclude the period, and state which.

---

## 6. Sportsbook — event-level exposure

Casino outcomes are independent across players, so player-level detection works. Sportsbook outcomes are **correlated**: a heavily-backed favourite winning hits every player who backed it simultaneously. Searching for outlier players will not find a bad results night.

Required additional views when sportsbook margin falls below the band:

1. **Top 10 settled events by net payout** for the period
2. **Largest liabilities by selection** — concentration of stake on single outcomes
3. **Pre-match vs. live split** of turnover and margin
4. **Single vs. accumulator split** — a shift toward singles compresses margin mechanically, independent of results

Distinguish these two conclusions explicitly, because they lead to different actions:

- **Results variance** — margin/pricing was correct, outcomes ran against the book. Self-correcting. No action.
- **Bet mix shift** — margin structure changed. Not self-correcting. Commercial or pricing action.

---

## 7. Governance

1. **Never delete.** Cap or exclude for the trend view only, and always present reported alongside adjusted with the delta named — "VIP impact", "jackpot impact", "sportsbook results impact". An unlabelled adjustment eventually becomes a way to explain away every bad number.
2. **Materiality gate.** Only adjust or flag if the adjustment moves the period-on-period delta by more than **2pp**. Below that, note and move on.
3. **Seasonality is not an outlier.** Baltic start-of-month payday effects, sporting calendar effects and holiday effects are known patterns. They are described as seasonality and are never flagged as outliers or trend reversals. A rule that flags known seasonality will destroy trust in every other flag it produces.
4. **Event months are flagged, never excluded from reporting.** They are excluded only from threshold and baseline *estimation*. The revenue is real; the tier mix is distorted. Both facts go in the pack (Section 2.6).
4. **Quarterly recalibration only.** Thresholds and baselines are recomputed on schedule. Never in response to a specific number.
5. **Deck claims are hypotheses.** Any margin or outlier claim originating in a presentation is verified against player-level data before it is repeated.
6. **Corrections generalise.** Any correction to this standard applies as a general rule unless explicitly scoped to one brand or market.

---

## 8. Required output format

For each brand-product in the pack:

```
Turnover:        [actual]  vs  [prior]     [Δ%]        [event month? Y/N]

  High value   count [n] (a:[x] b:[y])  ATPU [v]   turnover [t]   [Δ%]
  Regular      count [n]                ATPU [v]   turnover [t]   [Δ%]

  Decomposition of turnover Δ:
    HV count     [±pp]      HV ATPU     [±pp]
    Reg count    [±pp]      Reg ATPU    [±pp]

Margin:          [actual]  vs  [12m base]  [band P10–P90]  [in / out]
GGR:             [actual]  vs  [prior]     [Δ%]
Effective N (1/HHI): [n]        Gini: [g] (vs [3SD band])
```

For brand-products below the 1/HHI gate, replace the tier block with a named top-player attribution line and state that the base is too concentrated to segment.

Consistent with the standing presentation philosophy: general overview with 2–3 highlighted areas. This format exists to prevent misattribution, not to add slides. Lead with whichever of the four decomposition terms is doing the work.

---

## 9. Reconciliation with existing workspace code

Audited 2026-08-21. Two independent outlier implementations exist and disagree on every parameter.

| | `weekly_orchestrator.py` `get_outliers_iqr()` | `outlier_detection.py` `get_extreme_outliers()` |
|---|---|---|
| Metric | GGR | NGR |
| Bound level | Per brand | Per brand, or brand × product |
| Multiplier | 1.5× IQR | 3.0× IQR |
| Absolute floor | None | €1,000 |
| Window | Current week; history pooled across 13 weeks in one pass | Caller-defined |
| Disposition | Excluded from "Core GGR"; top 3 named in LLM prose | Label only |

**Verdict: standardise on `outlier_detection.py` and retire the orchestrator's version.** It is closer to correct on multiplier, floor, product grain and disposition.

### 9.1 Defects to fix independently of this standard

These are correctness issues in live reporting, not standard-adoption questions:

1. **Pooled 13-week IQR.** History bounds are computed on player-13-week totals while the current week uses player-week totals. Different units, different variance, different flagged population. The Core GGR vs 13-Week Core Average comparison is therefore not like-for-like and part of every WoW delta is definitional. Fix: compute per week, then average.
2. **1.5× with no floor.** Measure the flag rate and the share of GGR excluded before anything else. Above ~5% of players or ~10% of GGR, "Core GGR" is not measuring core.
3. **Flagged players narrated to the LLM.** The top 3 flagged players are handed to the summary step labelled 'High Win'. Since the flag is outcome-based, the LLM is being invited to write commercial causation around luck. This conflicts with the standing rule that behavioural claims must be verified against player-level data. Either stop passing them, or pass them labelled as margin events with no behavioural framing.

### 9.2 Changes this standard requires

1. **Metric switch, GGR/NGR → turnover** (Section 2). Outcome-based flags move to Layer 2 as margin variance at brand-product-period grain, not player grain.
2. **Product dimension in the weekly schema.** `weekly_orchestrator.py` has no product column at all, which blocks the Casino / Live Casino / Sportsbook split. This is a data-model change, not a parameter change, and is the largest single work item.
3. **Disposition.** Keep reporting an unadjusted total alongside any derived core — the orchestrator already does this and it is correct practice. Retain it.

### 9.3 What to keep unchanged

Player-level grain; both-tail detection; the absolute euro floor concept; `by_product=True` capability; dual reporting of actual and adjusted; `correction_log.md` as the log for baseline switches (3.3) and data errors (2.3).

**Parked decision, unchanged:** whether risk/integrity metrics become a permanent lead or secondary KPI category is still open. This standard does not resolve it and does not assume it.

---

## 10. Why abuse detection needs a separate standard

Running this standard for bonus abuse would systematically miss abusers. Four structural reasons:

1. **Magnitude vs. pattern.** Section 2 classifies players by *size*. Abuse is usually not large — it is many accounts operating at whatever stake the bonus terms make optimal. A high-value threshold is the wrong instrument entirely, and abusers will sit in the regular tier by design.
2. **Different unit.** The unit here is brand-product-period. For abuse it is the bonus instance and the player lifetime: wagering completion rate, bonus-to-cash turnover ratio, game selection toward low-variance or margin-exempt titles, bet sizing pinned to max-bet terms, deposit → bonus → withdraw with no cash play, play stopping the moment wagering clears, and cross-account overlap on device, payment instrument or play pattern.
3. **Opposite sign, opposite cost of error.** A high-value player is good news. An abuse flag is adversarial and accusatory, so it needs an explicit false-positive tolerance and an evidentiary standard before anyone acts on it. A threshold that is fine for "which tier is this player" is not fine for restricting an account.
4. **Different population.** Lithuania brands never receive bonuses, so LV/EE/FI/SE only. This standard covers all markets.

There is also an adjacency worth keeping separate deliberately: a player flagged as unusually high volume may be a **responsible gambling** concern rather than a commercial or integrity one. Those are three different conclusions from one signal and should never be collapsed into a single flag.

The shared foundation is the player-level data layer, not the detection logic. A sibling standard should reuse it and define its own rules.

---

## 11. Open items

**Blocking a correct pack**

- [ ] Fix the pooled 13-week IQR in `weekly_orchestrator.py` — affects live packs (9.1.1)
- [ ] Confirm bonus/cash turnover split availability at product level. Until then, no cross-brand margin comparison (3.6)

**Layer 2 build**

- [ ] Confirm 12 months of brand-product history exists on disk; if not, label the window actually used
- [ ] Establish whether sportsbook turnover is recorded on placement or settlement date
- [ ] Confirm sportsbook margin is gross or net of free bets, and whether that differs by brand
- [ ] Validate the ≥26-period and ≥20× turnover gates on real data — currently estimates (3.5)

**Data quality**

- [ ] Domain casing: `monthly_player_data.csv` uses lowercase, other pipeline files title-case. Any join silently drops rows
- [ ] `brand_9.bg` and `brand_10.net` have zero turnover everywhere — resolve at source or remove from the domain list
- [ ] Verify the turnover field is wagers/handle, not deposits, in every product table
- [ ] Confirm jackpot payouts are separately identifiable per period
- [ ] Establish whether live casino turnover is reported consistently across providers

**Analytical questions raised by the Layer 1 build**

- [ ] Test the casino cross-sell assumption for LV hockey events — the data does not obviously support it (2.6)
- [ ] `brand_1.lv` digital casino HV count fell ~15% from its late-2025 peak with regular base intact. Retention question, not a reporting one
- [ ] `brand_6_estonia` live casino collapsed ~84% in Aug 2025 when 2–3 players stopped. Below the segmentation gate, so it needs naming as attribution
- [ ] Sweden's NADP-to-actives ratio (~48%) against `brand_6se_sweden` being classified as passive — the two are hard to hold together
