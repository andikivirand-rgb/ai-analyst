# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An internal reporting/analytics pipeline for a group of iGaming/casino whitelabel brands (brand_1.lv/.ee/.lt/.com, brand_2.lv, brand_3.lv, brand_4.ee, brand_5.lt, Brand_6/Brand_7 brands, etc.). It pulls daily/monthly player and revenue data out of three production databases, aggregates it in pandas, and renders it into HTML/PDF KPI dashboards (weekly, monthly, YTD). `skills/*.py` are standalone pandas analysis modules (bonus abuse, outliers, cohorts, churn, LTV, segment/value trends) invoked directly — no agent framework, no LLM API.

**Policy: Claude only, no other LLM API.** A previous LLM-driven branch of the project (`autogen` agents backed by Gemini — a conversational analyst and a weekly-report orchestrator) was retired 2026-08-21 and is not to be revived. Its code was moved to `DO_NOT_USE/` (see `DO_NOT_USE/README.md`); `weekly_orchestrator.py` and its data source were deleted outright rather than moved. Do not import from or resurrect anything under `DO_NOT_USE/`.

There is no test suite, linter config, or `requirements.txt` in this repo — dependencies are only inferable from `import` statements (pandas, numpy, matplotlib, seaborn, scikit-learn, snowflake-connector-python, psycopg2, clickhouse-connect, python-dotenv, PyYAML, markdown, python-pptx). `pyautogen` and `xhtml2pdf` were only used by the retired `DO_NOT_USE/` pipeline. The venv at `venv/` is Python 3.13.

## Commands

Activate the venv first (Windows):
```
venv\Scripts\activate
```

Run the full standard reporting pipeline (brand KPIs → weekly player KPIs → weekly dashboard → YTD dashboard, in that fixed order — see `run_report.py`):
```
python run_report.py
```
This runs each step as `python -m <module>` and halts on the first failure. Note the monthly report step is currently commented out in `run_report.py`'s `modules_to_run` list.

Run an individual pipeline stage directly (must be run as a module, from the repo root, because scripts use `src.*` absolute imports):
```
python -m src.processing.brand_daily_kpis
python -m src.processing.monthly_actives_pull        # required before the monthly report generator
python -m src.generators.weekly_player_kpis
python -m src.generators.weekly_report_generator
python -m src.generators.monthly_report_generator
python -m src.generators.ytd_report_generator
```

There is no other pipeline in this repo — the Gemini-backed weekly-PDF orchestrator (`run_weekly_report.bat` / `weekly_orchestrator.py`) and the Gemini-backed conversational analyst (`python -m src.agents.analyst_agent`) were both retired 2026-08-21 (Claude-only policy). Do not recreate these commands or resurrect anything under `DO_NOT_USE/`.

## Architecture

### The one active pipeline (`data/raw` → `outputs/`)

**Extraction (`src/processing/`, `src/generators/weekly_player_kpis.py`)**: each script connects to Snowflake (Vendor_A-brand data, `VENDOR_A_WH.ANALYSIS.REVENUE`/`CURRENTPLAYER`), PostgreSQL (`transactions_casino_by_game`, `transactions_by_domain_product_user_daily`, `pbi_daily_kpis_report_*`, `budget_daily_report_y2023`), and ClickHouse (`BRAND_1.*` sportsbook/transfer/bonus tables) via `src/utils/db_connections.py`, and writes a flat CSV to `data/raw/`:
- `brand_daily_kpis.py` → `data/raw/brand_daily_kpis_comparison.csv` (brand/product/day, current + YoY columns, full re-pull every run)
- `weekly_player_kpis.py` → `data/raw/player_activity_master.csv` (player/day/product grain; **incremental** — caches previous pulls and only queries missing week-chunks within a rolling 12-week + 1-week-YoY window, then prunes to that window)
- `monthly_actives_pull.py` → `data/raw/monthly_actives_master.csv` (distinct active player IDs per month/domain/product, with strict per-domain source routing to avoid double-counting brands present in multiple DBs)
- `monthly_player_segmentation_pull.py` → `data/raw/monthly_player_data.csv` (month/player/product grain, NGR force-recomputed as `ggr - bonus_cost`)
- `brand_daily_actives_pull.py` → `data/raw/brand_daily_actives_comparison.csv` (day/domain/product grain, `product='Brand_Total'` rows are the true deduplicated-across-products actives). **Not wired into `run_report.py`** — not part of the active pipeline, run it manually when the true-overall actives figure is needed. `brand_daily_kpis.py`'s `actives_true_overall`/`actives_by_product` columns are populated by merging this file's output in; they read as all-zero if this script hasn't been run.

**Two different "actives" metrics exist — don't conflate them.** `brand_daily_kpis_comparison.csv`'s plain `actives` column (blank-`product` rows) is a **daily average**: `COUNT(DISTINCT external_id)` per day from `pbi_daily_kpis_report_actives`, then meaned across the days in a period — it is *not* deduplicated across days, so it answers "how big is a typical day," not "how many unique people played this month." A **monthly-unique** actives count (deduplicated across the whole month) is a different, usually much larger number. If an actives figure disagrees with another source, check which of these two it's computing (and what product scope) before assuming either is wrong.

None of these scripts read each other's output — they're independent extracts. The only ordering constraint is that `monthly_report_generator.py` requires `monthly_actives_master.csv` to already exist (run `monthly_actives_pull.py` first).

**Causal design pre-flight check (`src/utils/causal_design_check.py`):** before reporting a "did X cause Y" claim from a treatment/control or before/after comparison (e.g. "did a rule change increase engagement"), call `check_causal_design()`. It is deliberately not an estimator (no DiD/propensity-matching library) — three real DiD attempts on the 2026-09-07 Brand_2.lv Engage-multiplier question were tried and rejected (see `memory/feedback_causal_design_pitfalls.md`), because the standard toolkit assumes a real experiment this project's data usually doesn't have (whole-brand simultaneous exposure, no random assignment, shifting population composition). Instead it checks a proposed design against the three specific failure modes that analysis paid for: a frozen-snapshot group label tracked forward (mechanical regression-to-the-mean), a missing/empty stated reason for why the split corresponds to actual treatment exposure, and — the check that actually caught the real mistake — a placebo test rerunning the identical computation on brands with no intervention and flagging if they show a same-magnitude "effect" anyway. Manual/on-demand, like `reconcile_closed_domain_ytd()` — call it before finalizing a causal claim, not as part of any automated pipeline step.

**Data-integrity infrastructure (`src/utils/reconciliation.py`, `src/utils/provenance.py`):** `reconcile_pull()`/`reconcile_actives_pull()` cross-check a sample of a pull's turnover/GGR/actives cells against a live ClickHouse query before the output CSV is written (wired into `brand_daily_kpis.py`, `weekly_player_kpis.py`, `monthly_player_segmentation_pull.py`, `reactivation_events_pull.py`, `brand_daily_actives_pull.py` — not every pull script yet, adopt incrementally). `reconcile_closed_domain_ytd()` does the same for `brand_1.com` specifically (see the "closed brand" rule above) but is a manual/on-demand check, not run automatically, since that domain's history is static. Separately, `provenance.py`'s `logged_read_sql()`/`logged_ch_query()` are drop-in replacements for `pd.read_sql()`/`client.query()` that append the exact query text + row count to `data/logs/action_log_<date>.jsonl`, so a reported number can be traced back to the query that produced it — currently wired into `brand_daily_kpis.py`'s pull and both of `reconciliation.py`'s ClickHouse cross-check queries, not yet into every pull script. `src/utils/safe_query.py`'s `safe_read_sql()`/`safe_ch_query()` wrap those same two calls with a sqlglot read-only policy check (single SELECT/WITH statement only; no DML/DDL, SELECT INTO, row locks, or file/network table functions) before the query is sent — a client-side lock on top of the read-only sessions in `db_connections.py`, not the boundary. Use them for agent-written/ad-hoc SQL; adopt incrementally in pull scripts. Needs `sqlglot` (installed in `venv/`).

**Rendering (`src/generators/{weekly,monthly,ytd}_report_generator.py`)**: each reads `data/raw/brand_daily_kpis_comparison.csv` + `data/raw/player_activity_master.csv` (monthly also reads `monthly_actives_master.csv`), slices to the relevant period (current week/month/YTD vs. 12-week or 3-month baseline vs. YoY vs. budget), and hand-builds an HTML dashboard string (inline CSS, no template engine) written to `outputs/{weekly,monthly,ytd}/`.

These three generator files are near-duplicates of each other (same `COUNTRY_ORDER`/`BRAND_ORDER` lists, `get_country`/`get_brand_color`/`calc_var`/`format_var`/`get_extreme_outliers` helpers, same HTML/CSS skeleton) with only the period-slicing and baseline-averaging logic differing. **When fixing a bug or adding a brand/country in one, check whether the same change is needed in the other two** — there's no shared module they import from. After editing any of the three, run `python -m src.utils.generator_consistency_check` — it diffs these shared helpers/constants across all three files and fails if weekly and monthly diverge at all, or if ytd diverges from weekly by anything other than the documented `brand_1.com` YTD-only exception (see `config/business_rules.md`).

Outlier detection is duplicated a third time in `src/processing/outlier_detection.py` (`get_extreme_outliers()`, `get_core_frame_per_period()`, plus the P90-share/tiered-share/delta-impact functions added during the standard's Section 2.1 backtest below), unused by the three generators above, which still inline their own copy — that inconsistency is unresolved. A fourth, separate implementation used to live in `weekly_orchestrator.py`; that file was deleted 2026-08-21 (Claude-only policy — see below). See `config/standards/outlier_and_margin_variance_standard.md` (v2.0): Layer 1 (high-value segmentation — frozen per-brand-product turnover thresholds, four-term turnover decomposition, 1/HHI≥30 sample gate) is built and active in `src/processing/hv_segmentation.py` / `config/hv_segmentation.yaml`. Layer 2 (margin variance against a trailing baseline) is specified in the standard but not built. The standard's Section 2.1 table lists five detection rules — 3×IQR, P90-of-top-player-share, share-of-turnover tiers, per-player delta-impact (the functions living in `outlier_detection.py` above) — that were built, backtested against 19 months of real data, and disproved: **do not reimplement or extend them.**

### `skills/*.py` — standalone analysis modules

`skills/bonus_abuser_flag.py`, `outlier_detection.py` (actually lives at `src/processing/outlier_detection.py`, not `skills/` — despite the name pattern, don't assume it's there), `cohort_analysis.py`, `churn_predictor.py`, `ltv_calculator.py`, and `segment_value_analysis.py` are plain pandas functions with no agent/LLM dependency — invoke them directly from a script (`from skills.bonus_abuser_flag import flag_bonus_abusers`, etc.) against `data/raw/*.csv` or a fresh live pull. This is the active pattern for ad-hoc analysis (bonus abuse, outliers, segment/value trends) going forward — there is no conversational/agent-based way to run these, by design (Claude-only policy, see below).

### Retired: Gemini-backed pipelines

Two things used to exist here and were retired entirely on 2026-08-21 because the project uses **Claude only, no other LLM API**:
- A conversational analyst (`autogen` Planner → DataAnalyst → ChartDesigner → ManagerReviewer, Gemini-backed) that operated on an anonymized CSV extract.
- A single-script weekly PDF report orchestrator (its own outlier/GGR-vs-Core-GGR math, one Gemini `ManagerReviewer` agent).

`weekly_orchestrator.py`, `run_weekly_report.bat`, and their shared data source (`Anonymized_Daily_Stats.csv`) were deleted outright. The conversational analyst and its support code (`analyst_agent.py`, `memory_manager.py`, the `anonymize_data.py`/`fix_csv.py`/`format_large_csv.py` utilities that only fed it, and its `memory_bank.json`/`analyst_workspace/` artifacts) were moved to `DO_NOT_USE/` rather than deleted — see `DO_NOT_USE/README.md`. Do not resurrect either pattern; if a conversational or automated-report feature is wanted again, build it against Claude and reuse `skills/*.py` + `data/raw/*.csv`, not this pattern.

### Configuration

- `config/.env` — DB credentials (`SF_*` Snowflake, `PG_*` PostgreSQL, `CH_*` ClickHouse). Loaded explicitly by path (`config/.env`) in the processing/generator scripts, but by bare `load_dotenv()` (CWD-relative) in `src/utils/db_connections.py` — be aware of this inconsistency if a script fails to find credentials depending on where it's invoked from. (`GEMINI_API_KEY`, if still present in this file, is unused — only the retired `DO_NOT_USE/` code ever read it.)
- `config/company_context.yaml` — KPI glossary (activity/payment/financial/gameplay measures with exact formulas) and design/brand guidelines (colors, chart style, brand color hex codes). Treat this as the single source of truth for KPI definitions and brand styling — don't let generated code invent its own metric formulas. (It used to also be injected into the retired LLM agents' prompts; that consumer no longer exists, but the file's content is still authoritative for KPI/brand definitions.)
- `config/standards/` — longer-form methodology standards, distinct from `business_rules.md` (which documents known intentional exceptions) and `company_context.yaml` (which is a KPI glossary). Currently contains `outlier_and_margin_variance_standard.md` (v2.0), which separates behavioural player movement (Layer 1) from stochastic/luck margin variance (Layer 2):
  - **Layer 1 — built and active.** `src/processing/hv_segmentation.py` + `config/hv_segmentation.yaml` implement frozen-threshold high-value/regular player segmentation and the four-term turnover decomposition, gated per brand-product by a 1/HHI≥30 sample-size check (brand-products below that are skipped, not force-fit). This is the single-track, per-brand-product turnover model — kept in place unchanged.
  - **Two-track HV/regular model — built and additive.** `src/processing/hv_two_track_segmentation.py` + `config/hv_segmentation_two_track.yaml` implement a *separate* per-brand (not per-brand-product) model with two never-summed tracks — casino (theo GGR) and sportsbook (turnover) — same frozen-threshold/four-term-decomposition logic, plus per-player `casino_tier`/`sb_tier`/`overall_tier`/`is_cross_seller` and a cross-seller value-share report. See Scope, Player value, and Data sources below.
  - **Layer 2 proper — margin variance against a trailing baseline — still not built.** The two-track model above segments player *value*; it does not itself benchmark actual margin against a trailing baseline (that's a distinct, still-open piece).
  - The standard's Section 2.1 table records five detection rules (3×IQR, P90-of-top-player-share, share-of-turnover tiers, per-player delta-impact) that were built, backtested against 19 months of real data, and disproved — **do not reimplement them.**
  - Explicitly out of scope for bonus abuse/fraud/risk-integrity detection (see its Section 10) — don't reuse this standard's thresholds for that; it needs its own standard.
  - **Scope.** 12 in-scope domains: `brand_1.lv`, `brand_3.lv`, `brand_2.lv`, `brand_1.ee`, `brand_4.ee`, `brand_6_estonia`, `brand_7_estonia`, `brand_1.lt`, `brand_5.lt`, `brand_6_finland`, `brand_7_finland`, `brand_6se_sweden`. `brand_2.ee` does not exist — don't treat it as a typo target. `brand_1.com` is closed (Finland), YTD-only, and retained in config as an explicit skip rather than removed. Fully out of scope: `brand_8_estonia`, `brand_8_finland`, `brand_8_malta`, `brand_9.bg`, `brand_10.net`. Brand-products where 1/HHI < 30 are dropped from the pack, not attributed to it — product owners know their own VIPs and don't need a low-sample segmentation forced on them.
  - **Player value.** Value players on theo GGR (using GGR margin), not NGR — bonus impact is a separate analysis run against real GGR, not folded into player value. Theo margin is estimated per brand-game, and a brand-product's margin follows from its game mix (games are configured per brand, so there's no cross-brand pooling of margin). Sportsbook is excluded from theo margin entirely — margin depends on what players actually bet on, so there's no fixed edge to estimate — and keeps its existing turnover-vs-margin treatment instead.
  - **Data sources.** The game-level pull (`src/processing/game_level_ggr_pull.py` → `data/raw/game_level_brand_month.csv`, brand × product × game × month grain) has been run for the 12 in-scope domains above plus `brand_1.com` (present per its usual closed/YTD-only treatment), covering 2024-01 through 2026-07. ClickHouse (`transactions_npl_casino`, `transactions_atomic_slots`, `transactions_atomic_live`) is the trustworthy source for this work. For `transactions_npl_casino`: `turnover = SUM(bet)`, `ggr = SUM(output) - SUM(input)` — do **not** use `bet - win`, since `SUM(win)` is silently zero on this table and that formula returns turnover equal to GGR. Baseline year for frozen thresholds is 2024, except `brand_6se_sweden`, which uses 2025 (its casino product only starts May 2024, and it has no sportsbook). `monthly_player_data.csv` uses lowercase domain casing while the rest of the pipeline uses title-case — any join between them needs an explicit casing mapping or it will silently drop rows.
  - **Theo margin table.** `src/processing/theo_margin_table.py` → `data/raw/theo_margin_table.csv` (per brand-product-game turnover-weighted margin, full-period 2024-01–2026-07, games below €10k full-period turnover fall back to a brand-product average). Every margin is **floored at 2%** (`MARGIN_FLOOR`) — no casino game has a genuine long-run non-positive edge; a raw aggregate margin at or below that in the estimation window is short-run variance, not the game's true edge, and leaving it unfloored silently excludes real-turnover players from HV/regular tiering (theo GGR ≤ 0 despite turnover > 0). Whenever this table changes, every downstream artifact must be re-derived in order: `src/processing/player_game_ggr_pull.py` (player × game pull, only needed if the game set itself changes) → `src/processing/player_casino_value_combine.py` (player-month theo GGR) → the two-track model's frozen thresholds (recompute P99/1-HHI per brand, baseline 2024) → re-run `hv_two_track_segmentation.py`'s reports. It is not an isolated tweak.
  - **Sportsbook has no "theo" anything.** Its value metric is plain turnover — never call it "theo turnover" or similar; there's no theoretical edge to estimate for sportsbook (margin depends on what a bettor bets on).

## Bi-Weekly Brand Analysis Logic

When preparing or reviewing the bi-weekly brand performance discussion, apply this analysis logic rather than treating every KPI as equally important:

**Lead KPIs (check first, per brand):**
- GGR/NGR
- Actives / Retention

Deposits, ADPU, and Bonus cost % are secondary/supporting metrics — useful for explaining *why* a lead KPI moved, but not headline numbers in their own right.

**Open decision — risk/integrity metrics:** whether risk/integrity metrics (bonus abuse, outlier concentration) should become a permanent lead or secondary KPI category here has not yet been decided. Until that's settled, treat player segmentation / bonus-abuser / outlier analysis as a special report type (see Memory Protocol below), not as an extension of the regular bi-weekly Lead KPI framework — don't retrofit it into "lead" or "secondary" without revisiting this decision first.

**Presentation structure:** one brand at a time, grouped by country, in the same order as the live deck: Latvia brands together, then Estonia, then Lithuania, then Finland, then Sweden. This mirrors the `COUNTRY_ORDER`/`BRAND_ORDER` grouping already used in `weekly_report_generator.py`/`monthly_report_generator.py`/`ytd_report_generator.py`.

**Flagging logic — what's worth raising in the meeting:** a brand is worth flagging if *either* of the following shows a notable deviation (not both):
1. A recent performance trend change — WoW or MoM shift in GGR/NGR or Actives (vs. the 12-week/3-month baseline the generators already compute).
2. YTD performance vs. budget — a notable miss or beat on the budget variance figures already in the YTD dashboard.

**Apply `config/business_rules.md` context before flagging anything.** Specifically:
- Don't flag `brand_6.se` (Sweden) declining or flat Actives/GGR as alarming — it's a passive brand with no marketing/feature investment and no bonus lever, so stagnation/decline is expected, not a problem to investigate.
- Don't flag Lithuania (`brand_1.lt`, `brand_5.lt`) bonus metrics reading 0/N/A as a data quality issue — bonuses are regulatorily prohibited there.
- Don't flag `brand_1.com` (Finland) being absent from weekly/monthly numbers, or present in YTD, as inconsistent — it's a closed brand handled intentionally per the rules above.

**Causal breakdown standard (adopted from BetCity-style trading decks):** when NGR/GGR misses or beats budget or trend, don't stop at one summary bullet — break the driver down into its component factors and quantify each one:
- For Casino: **Actives × ATPU × Margin** — state which factor(s) actually moved and by how much, and which stayed flat/on-budget.
- For Sportsbook: **Turnover × Margin** — state whether a miss was a volume problem or a margin problem, since they require different explanations and actions.

Example of the standard: *"NGR missed budget by X. This was NOT a margin issue (margin was in line/above budget at Y%) — it was driven entirely by Actives falling Z% below target."* Avoid vague single-cause statements like "revenue dropped due to lower engagement" without naming which specific factor moved.

**Value segmentation within the causal breakdown:** once a factor above is identified as the mover, go one level deeper using the value segmentation (methodology and thresholds in `config/standards/outlier_and_margin_variance_standard.md` — don't restate that detail here):
- Split Actives and ATPU into HV and regular tiers, and use the four-term decomposition to state whether the move came from tier count or value per player, not just "ATPU moved." Casino tiers are scored on theo GGR; sportsbook tiers are scored on turnover — the two tracks are never summed, the units aren't comparable.
- Report margin against its trailing baseline and band rather than folding it into the ATPU story, so a lucky/unlucky month reads as variance, not a behavioral shift.
- Where cross-seller activity is moving, note the cross-seller count and their share of casino/sportsbook value alongside the tier breakdown.
- Brand-products below the 1/HHI ≥ 30 sample gate are dropped from the pack for this purpose, not attributed a tier — don't force a segmentation read onto too small a sample.
- **Lead with YoY, not month-on-month, for this decomposition.** Sportsbook results — and their cross-sell lift into casino — are seasonal (major tournaments, e.g. a World Cup falling in a given June-July), so a single-month-over-month view can show the opposite driver from the YoY view (seen concretely on brand_3.lv: MoM said "no new players, all value-per-head"; YoY showed regular player count was the largest driver). Check whether MoM and YoY agree before defaulting to MoM, and prefer YoY when they diverge.

This stays within the existing presentation philosophy (general overview, 2-3 main areas per brand) — it's a deeper *explanation* of an already-identified mover, not a new exhaustive pass to run on every brand every time.

**Seasonality/timing check (general rule):** before treating any WoW uptick or start-of-month strength as a real trend reversal, check monthly/daily phasing first. In the Baltics, start-of-month performance is typically stronger due to payday timing — this is a known, recurring pattern, not a signal on its own. Summer months also tend to run slower overall. A single strong week at month-start should be flagged as "possible payday effect, needs longer confirmation," not presented as a turnaround.

**Cross-sell effect (general rule):** when Sportsbook activity spikes (e.g. major tournaments like the World Cup), expect a correlated lift in Casino activity from cross-sell — the two are not independent signals. Don't attribute a simultaneous Casino uptick purely to Casino-specific factors without first checking whether it coincides with a Sportsbook spike.

**VIP/high-value player attribution (methodology rule):** before presenting a narrative like "VIP replacement problem" or attributing a revenue change to specific player behavior (e.g. "a VIP left" or "retained VIPs cut spending"), verify this against actual player-level data (e.g. the per-player-granularity data files, not just aggregate KPI tables) rather than inferring it purely from aggregate KPI movement. State explicitly whether a claim about specific players is confirmed from player-level data or is an inference from aggregates.

**Presentation philosophy:** when narrowing a brand write-up down to its 2-3 most important areas (per the Lead KPIs / Flagging logic above), don't silently decide what to leave out. After drafting the analysis, briefly state what was deprioritized and why (e.g. smaller absolute NGR base, same root cause as a headline item already covered, a secondary/supporting metric per the Lead KPIs rule) — then ask the user whether any of it should be included before treating the write-up as final.

## Memory Protocol

**Before starting any new bi-weekly brand analysis**, always read `memory/past_meetings/` first — all subfolders, most recent 3-4 meetings at minimum (folders are named `YYYY-MM-DD_brandname/`, so sort by the date prefix to find the most recent). From each folder, extract:
1. What was flagged/discussed for that brand.
2. Any figures or trends mentioned.
3. Any corrections the user made during that session — check that folder's `corrections.md` if present, and cross-reference `memory/correction_log.md` for the master list of corrections across all past sessions.

**After completing a new analysis, before ending the session**, ask the user: *"Should I save this session's analysis and any corrections to memory/past_meetings/ for next time?"* If yes:
1. Create a new dated subfolder under `memory/past_meetings/` following the existing `YYYY-MM-DD_brandname/` convention (use the brand/meeting discussed that session; if multiple brands were covered, either use a combined folder name or create one per brand — match whatever grouping the session actually covered).
2. Save the analysis output produced that session into that folder.
3. If the user corrected or overruled anything during the session, save a `corrections.md` file in that same folder listing each one in this exact format:
   ```
   Original: [what I said] | Correction: [what the user said] | Rule: [general rule to apply going forward]
   ```

**Whenever a correction is saved to a dated subfolder**, also append a one-line summary of it to `memory/correction_log.md` (the running master list), so corrections can be scanned quickly without opening every past folder.

**Segmentation / special reports** — non-recurring, non-bi-weekly report types (e.g. player segmentation, bonus-abuser/outlier analysis) save under `memory/past_meetings/YYYY-MM-DD_brandname_segmentation/` instead of the regular `YYYY-MM-DD_brandname/` convention. This keeps them out of the regular bi-weekly meeting folder sequence for the same brand/date, so a segmentation report and a bi-weekly review landing on the same day for the same brand don't collide or get mixed up when reading recent history back in.

## Data Source Rules

- **`memory/past_meetings/2026-08-13_brand_1-estonia/Result_3.xlsx` and `memory/past_meetings/2026-08-13_brand_1-estonia/data (5).xlsx` are NOT reliable/authoritative sources.** Do not read or cite either file in any future analysis — treat them as retired, even though they remain in that meeting folder for historical record.
- For player registration and account-blockage data going forward, use **`data/reference/Registrations and blockages.xlsx`** instead. This file was generated on 2026-08-12.

## Presentation Style Reference

**`data/reference/[COMPANY] PPT Template.pptx`** is the company-branded PowerPoint template. Any slide deck produced for this project (bi-weekly reviews, bonus-abuse/segment writeups, etc.) should follow this deck's style rather than default/plain PowerPoint formatting. It's a full [COMPANY]-branded template (27 example slides + 36 slide layouts, widescreen 13.33" × 7.5"), not just a title page — read it directly (`python-pptx` is available in `venv/`) before building a deck rather than relying only on this summary, since exact positions/colors are easiest to pull straight from the layouts. **Also read an actual past example output deck** (e.g. `memory/past_meetings/2026-08-25_brand_3-lv/Brand_3_LV_Biweekly_Review_2026-08-25.pptx`, or the Brand_1 Estonia one under `2026-08-13_brand_1-estonia/`) for structural/content patterns — the blank template's layouts alone don't tell you which layout to use for what, or in what order sections should run.

When starting from the template in code (python-pptx), the 27 example slides it ships with need removing before adding new ones — clear `presentation.slides._sldIdLst` AND call `presentation.part.drop_rel(sld.rId)` for each removed slide, or the orphaned slide parts stay in the saved package and collide with the new slides' auto-generated part names. Also: not every layout that *looks* branded actually has a background fill — layout 26 ("Section Header") has none (renders as plain white, so white divider text on it is invisible). Layouts 9-14 all have a solid-fill background, but **not all of them are purple** — check the actual scheme color, not just the name or position in the list: layout 12 ("Divider 3 – Play texture") fills with `bg2` (a light, near-white gray-blue), so white divider text is just as invisible on it as on layout 26 despite looking like the other "real" dividers by name. **Use layout 11 ("Divider 2 – Play texture") or layout 13 ("Divider 1 – Simple texture") instead** — both fill with `accent1`, the actual purple `#8846E6`. (Confirmed 2026-09-12: `build_deck.py`, `build_deck_brand_6_estonia.py`, and the original `build_deck_reactivation_followup.py` all used layout 12 for their dividers — the Brand_3 and Brand_6 Estonia decks likely have the same invisible-divider-title bug; `build_deck_reactivation_followup.py` has been fixed to use layout 11, the other two have not been checked/fixed as of this note.) Don't trust a layout's name or its inclusion in a "these look branded" range — read `layout.background`'s actual `<a:solidFill><a:schemeClr val="...">` before using any layout for white/light text.

**Currency: use the `€` symbol, never the string "EUR"**, in any deck, chart, or chart title.

**No methodology narration in a finished deck.** A deck tells the end user what the numbers are and what they mean — never how the analysis was built, what a prior version got wrong, what file/rule/table something came from, or that a chart was rebuilt because an earlier method was flawed. That belongs in this file, `correction_log.md`, or the chat response, not on a slide. Genuine *interpretive* caveats stay (e.g. "only 2-7 players — read with caution" helps the reader weigh the number correctly); *process* commentary goes (e.g. "note: the pipeline's actives column is unreliable for this brand" or "the previous file used the old method and has been rebuilt" — fix the number, don't narrate the fix).

**Two segmentation tools, not one replacing the other — say so if asked, don't just silently switch.** `get_extreme_outliers` (outlier detection) deliberately scores players on **turnover**, never GGR/NGR/theo GGR — mixing in a GGR-based metric would conflate behavioral extremity (how much someone plays) with stochastic luck (whether they won or lost that period), which is exactly what it's designed to avoid. The HV/regular two-track model scores casino players on **theo GGR** because that's a value classification of the whole active population, a different job than flagging outliers. Both are correct, for different questions — never treat building/using one as having deprecated the other.

**Colors** (from the theme, `ppt/theme/theme1.xml`):
- Dark/text: `#16091C` (near-black purple)
- Light background: `#FFFFFF` (white), `#EEF1F4` (light gray-blue)
- Primary accent: `#8846E6` (purple) — main brand accent, used for hyperlinks/highlights too
- Secondary accent: `#00E6C3` (teal/cyan)
- Deep accent: `#29007D` (deep purple)

**Fonts:** Mulish-Bold for headings/titles, Mulish-Regular for body copy.

**Logo placement:** a small logo mark/wordmark sits top-left on every single layout (content slides: a small mark around L=0.33", T=0.40", ~0.2"×0.4"; title/divider slides: a larger wordmark around L=0.5", T=0.4", ~2.2"×0.7"). Never place other content there.

**Recurring structural elements:**
- **Slide number**: fixed top-right on every non-title layout, ~L=12.1", T=0.4".
- **Title slides**: large title block centered-left (L=0.5", T=2.1", W=8.2", H=2.6") over a full-bleed graphic/texture background, subtitle/body copy below it (T=4.9").
- **Divider slides**: same large title block, plus a small running-header text top-left ("Title of document / running heading / date") next to the logo — used to mark section breaks. Use layout 9-14 (e.g. 12), not 26 — see the background-fill note above.
- **Content slides**: title top-left (L=0.8", T=0.3", one line, two max), one line of intro copy directly below (T=1.3"), body content starts at T=2.1", slide number top-right.
- **KPI card / pull-out layout** ("Content – 1 col + 4 pull outs"): the template's built-in 2×2 grid of callout cards (~4.1"×2.3" each, `[big stat number] | [subheading] | [supporting body copy]`). Usable for a single-topic "headline KPIs" slide, but for a full bi-weekly/trading deck prefer the structure below instead — the 2×2 grid reads as cramped once there's real narrative to carry.
- **Preferred bi-weekly/trading deck structure** (matches the actual Brand_1 Estonia and Brand_3.lv example decks, not a built-in template layout — build with custom textboxes/shapes on a "Title Only" layout): divider slides between major sections (YTD → latest month/MTD → product deep-dives → root-cause-of-miss), each content slide with a bold one-line takeaway headline under the title, then **4 KPI cards stacked in a narrow left column** (not 2×2) plus **two wider narrative columns** on the right, each with a bold sub-header and several short dash-bullets. Charts (matplotlib PNGs inserted via `add_picture`) replace one narrative column when a trend or decomposition visual is the point of the slide.
- **Chart/table slides**: each chart or table has its own small header directly above it in the pattern `Title one line of copy (two if no sub copy below) | subcopy one line`, then the chart/table itself below. Layouts exist for 1 chart + notes column, 2 charts side by side, and full-slide chart/table grids.
- **Quote slide**: large pull-quote (up to 7 lines) against a full-bleed image, running-header + slide number in the same fixed positions as content slides.
- **End slide**: "THANK YOU." on a dark-purple/purple textured background, contact details below it.
