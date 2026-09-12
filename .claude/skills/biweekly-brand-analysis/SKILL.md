---
name: biweekly-brand-analysis
description: Use when preparing, running, or reviewing a bi-weekly (or monthly/YTD) brand performance analysis or discussion for any brand_1/brand_2/brand_3/brand_4/brand_5/brand_6/brand_7 brand — GGR/NGR review, actives/retention review, causal breakdown of a KPI miss/beat, or building the review deck. Enforces this project's Lead-KPI framework, causal decomposition standard, seasonality checks, and memory protocol so they aren't skipped.
---

# Bi-Weekly Brand Analysis

This project's methodology lives in `CLAUDE.md` ("Bi-Weekly Brand Analysis Logic" and "Memory Protocol" sections) and in `memory/correction_log.md`. This skill turns that into an enforced checklist for every session — work through it in order, don't skip steps just because the request looks like "just give me the numbers."

## 0. Before starting: read memory
- Read `memory/past_meetings/` — most recent 3-4 dated folders at minimum (sort by the `YYYY-MM-DD` prefix). For each: what was flagged, what figures/trends were discussed, and check for a `corrections.md`.
- Skim `memory/correction_log.md` — both for the specific brand(s) in scope this cycle, and for general pipeline/methodology entries, since those apply regardless of which brand is up this cycle.
- If this is a segmentation/bonus-abuse/outlier special report rather than the regular bi-weekly cycle, its folder lives at `YYYY-MM-DD_brandname_segmentation/` instead — check there.

## 1. Scope and order
- Lead KPIs, checked first, per brand: **GGR/NGR** and **Actives/Retention**. Deposits, ADPU, and bonus cost % are secondary/supporting — they explain the "why," they are never the headline.
- Present one brand at a time, grouped by country, in this fixed order: Latvia → Estonia → Lithuania → Finland → Sweden (matches `COUNTRY_ORDER`/`BRAND_ORDER` in the report generators).

## 2. Apply business-rule context before flagging anything
- `brand_6.se`: passive brand, no marketing/feature investment, no bonus lever — flat or declining Actives/GGR is expected, not a flag.
- `brand_1.lt` / `brand_5.lt`: bonuses are regulatorily prohibited there — 0/N/A bonus metrics are not a data-quality issue.
- `brand_1.com`: closed brand, YTD-only — its absence from weekly/monthly numbers is intentional, not inconsistent. If a YTD figure that includes `brand_1.com` (Finland country totals, Overall totals, Snapshot table) looks off, validate it with `reconcile_closed_domain_ytd()` in `src/utils/reconciliation.py` before treating it as a pipeline bug — it's a manual/on-demand check (not run automatically), since this domain's history is static and has no ongoing regression to catch. Treat a first-run `FAIL` from it as "needs investigation," not automatic proof of a bug — its 1% tolerance was backtested on the other 7 brand_1-family domains, not on `brand_1.com` itself.

## 3. Flag only on a real deviation
Flag a brand if *either*:
1. A WoW/MoM trend shift in GGR/NGR or Actives vs. the 12-week/3-month baseline the generators already compute, or
2. A notable YTD miss/beat vs. budget.

Before calling anything a trend: check for Baltics payday timing (start-of-month strength is a known recurring pattern) and summer seasonality first — a single strong week at month-start is "possible payday effect, needs longer confirmation," never presented as a turnaround. Also check whether a Casino uptick coincides with a Sportsbook spike (tournament cross-sell) before attributing it to Casino-only factors — the two are not independent signals.

## 4. Causal breakdown of any flagged miss/beat
Don't stop at one summary bullet. Decompose the driver and quantify each term:
- Casino: **Actives × ATPU × Margin**
- Sportsbook: **Turnover × Margin**

State which factor(s) actually moved and by how much, and which stayed flat/on-budget. Avoid vague single-cause statements like "revenue dropped due to lower engagement" without naming the specific factor.

Once a factor is identified as the mover, go one level deeper with the value segmentation (methodology in `config/standards/outlier_and_margin_variance_standard.md` — don't restate the detail here, just apply it):
- Split Actives and ATPU into HV vs. regular tiers via the four-term decomposition — state whether the move came from tier *count* or value *per player*, not just "ATPU moved."
- Casino tiers score on theo GGR, sportsbook tiers score on turnover — the two tracks are never summed.
- Report margin against its trailing baseline/band rather than folding it into the ATPU story, so a lucky/unlucky month reads as variance, not a behavioral shift.
- Note cross-seller count and their value-share where cross-sell activity is moving.
- Brand-products below the 1/HHI ≥ 30 sample gate are dropped from the pack for this purpose, not force-tiered.
- **Lead with YoY, not MoM.** Sportsbook and its casino cross-sell lift are seasonal (major tournaments); MoM and YoY can show opposite drivers (seen concretely on brand_3.lv — MoM said "no new players, all value-per-head," YoY showed regular player count was the largest driver). Check whether MoM and YoY agree before defaulting to MoM, and prefer YoY when they diverge.

This is a deeper *explanation* of an already-identified mover, not a new exhaustive pass to run on every brand every cycle — stays within "general overview, 2-3 main areas per brand."

## 5. Reporting discipline (recurring corrections from `memory/correction_log.md`)
- State product scope (digital casino / live casino / sportsbook / brand-total) on every KPI figure, every time it's quoted — never rely on scope stated once earlier in the message to carry through later sentences. A bare "Casino" figure must never silently mean "Digital Casino only."
- In any multi-brand comparison, show every brand's figure explicitly side by side — never let one brand's number carry the narrative while another's is buried or only mentioned in passing.
- Before citing "actives," confirm whether the source is a daily-average or a monthly-unique figure (see `CLAUDE.md`'s "Two different actives metrics" note) — they legitimately disagree and neither is wrong on its own; state the exact formula/scope when reconciling against another source.
- Before attributing a change to specific player/VIP behavior (e.g. "a VIP left," "retained VIPs cut spending"), verify against player-level data rather than inferring from aggregate movement — state explicitly whether the claim is confirmed or inferred.
- Use `€`, never the string "EUR," anywhere the analysis becomes a chart, table, or deck.

## 6. Deprioritization transparency
When narrowing a brand write-up to its 2-3 most important areas, don't silently decide what to leave out. After drafting, briefly state what was deprioritized and why (smaller absolute base, same root cause as a headline item already covered, or a secondary/supporting metric per the Lead-KPI rule) — then ask the user whether any of it should be included before treating the write-up as final.

## 7. After the analysis: offer to save to memory
Ask: *"Should I save this session's analysis and any corrections to memory/past_meetings/ for next time?"* If yes, follow `CLAUDE.md`'s Memory Protocol exactly — dated folder (`YYYY-MM-DD_brandname/`, or `_segmentation` suffix if applicable), a `corrections.md` in the `Original: [...] | Correction: [...] | Rule: [...]` format for anything the user overruled, and a one-line append to `memory/correction_log.md` for each correction.
