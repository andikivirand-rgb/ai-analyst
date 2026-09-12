# AI Analyst Workspace

An internal reporting/analytics pipeline for a group of iGaming/casino whitelabel brands (brand_1.lv/.ee/.lt/.com, brand_2.lv, brand_3.lv, brand_4.ee, brand_5.lt, Brand_6/Brand_7 brands, etc.). It pulls daily/monthly player and revenue data out of three production databases, aggregates it in pandas, and renders it into HTML/PDF KPI dashboards (weekly, monthly, YTD). Built and operated through Claude Code — **Claude only, no other LLM API** (see Policy below).

> **This repository intentionally contains code and methodology documentation only.** Real DB credentials, live data pulls, rendered reports, and business meeting notes are excluded via `.gitignore` and exist only in the private working environment this repo is developed from. Nothing in this repo can connect to a live database or reproduce a real figure on its own.

## Architecture

### Extraction (`src/processing/`, `src/generators/weekly_player_kpis.py`)
Each script connects to Snowflake (Vendor_A-brand data), PostgreSQL, and ClickHouse via `src/utils/db_connections.py`, and writes a flat CSV to `data/raw/` (not included in this repo). Full details of each pull script — grain, incremental caching strategy, known metric-scope gotchas — are in `CLAUDE.md`.

### Rendering (`src/generators/{weekly,monthly,ytd}_report_generator.py`)
Each reads the extraction-layer CSVs, slices to the relevant period (current week/month/YTD vs. a rolling baseline vs. YoY vs. budget), and hand-builds an HTML dashboard (inline CSS, no template engine). These three files are near-duplicates of each other by design (no shared module), which is why `src/utils/generator_consistency_check.py` exists — see below.

### `skills/*.py` — standalone analysis modules
Plain pandas functions with no agent/LLM dependency (bonus abuse flagging, outlier detection, cohort analysis, churn prediction, LTV, segment/value trends) — invoked directly against pull-layer CSVs or a fresh live pull. No conversational/agent-based interface by design (see Policy).

### Data-integrity infrastructure (`src/utils/`)
Built to close specific, previously-encountered failure modes rather than as generic tooling:

- **`reconciliation.py`** — cross-checks a sample of a pull's turnover/GGR/actives cells against a live, independent ClickHouse query *before* the pull's output file is written, and raises (blocking the write) if a sampled cell drifts beyond a backtested tolerance. Grew out of a real incident where an incomplete domain-exclusion list silently double-counted several brands' turnover/GGR for an extended period, undetected. `reconcile_closed_domain_ytd()` handles the one domain (a closed brand) that's excluded from the standard weekly/monthly sample pool but still feeds YTD aggregates — a manual, on-demand check rather than a per-run gate, since a closed brand's history doesn't change.
- **`provenance.py`** — drop-in replacements for `pd.read_sql()`/ClickHouse's `client.query()` that log the exact query text, row count, and calling script to a dated JSONL file, so a reported number can be traced back to the query that produced it. Adopted incrementally at the pull scripts that most directly feed report figures, not yet everywhere.
- **`generator_consistency_check.py`** — the three report generators above intentionally share no code, so a bug fix or new-brand addition made in one can silently fail to propagate to the other two. This diffs their shared helper functions/constants and fails if two generators diverge with no documented reason, or if the one legitimate exception (a closed brand handled differently in YTD only) grows beyond its known scope.
- **`causal_design_check.py`** — a pre-flight check for "did X cause Y" claims (rule changes, campaigns), built from three real difference-in-differences attempts that were tried and rejected on this project's data. Rather than an estimator library, it checks a proposed design against the specific failure modes that analysis surfaced: a frozen-snapshot group label mechanically producing regression-to-the-mean, a missing justification for why a split corresponds to real treatment exposure, and an automated placebo test rerunning the same computation on unaffected comparison brands.

### Claude Code integration (`.claude/skills/`, `CLAUDE.md`)
Project-specific analysis methodology (KPI prioritization, causal decomposition standard, seasonality/payday checks, memory protocol for recurring bi-weekly brand reviews) is encoded as a triggered Claude Code skill (`.claude/skills/biweekly-brand-analysis/`) rather than left as prose that has to be manually recalled every session.

## Policy: Claude only, no other LLM API

A previous LLM-driven branch of this project (`autogen` agents backed by Gemini — a conversational analyst and a weekly-report orchestrator) was retired and is not to be revived. Its code was moved to `DO_NOT_USE/` (excluded from this repo) rather than deleted, as a record of what not to resurrect. If a conversational or automated-report feature is wanted again, it should be built against Claude and reuse the existing `skills/*.py` + pull-layer CSV pattern, not that architecture.

## Commands

Activate the venv first, then run the full standard reporting pipeline (brand KPIs → weekly player KPIs → weekly dashboard → YTD dashboard, in that fixed order):

```
venv\Scripts\activate
python run_report.py
```

Individual pipeline stages can be run directly as modules from the repo root (required, since scripts use `src.*` absolute imports):

```
python -m src.processing.brand_daily_kpis
python -m src.processing.monthly_actives_pull        # required before the monthly report generator
python -m src.generators.weekly_player_kpis
python -m src.generators.weekly_report_generator
python -m src.generators.monthly_report_generator
python -m src.generators.ytd_report_generator
```

After editing any of the three report generators, run the consistency check:

```
python -m src.utils.generator_consistency_check
```

There is no test suite, linter config, or `requirements.txt` — dependencies are only inferable from `import` statements (pandas, numpy, matplotlib, seaborn, scikit-learn, snowflake-connector-python, psycopg2, clickhouse-connect, python-dotenv, PyYAML, markdown, python-pptx).

## Documentation

`CLAUDE.md` is the full, authoritative reference for this codebase — architecture, known metric-definition gotchas, per-brand business rules, and the bi-weekly analysis methodology. It's written for an AI coding agent operating in this repo, but is equally the best starting point for a human reader.
