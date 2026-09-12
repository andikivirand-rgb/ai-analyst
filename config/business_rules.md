# Business Rules & Known Intentional Exceptions

This file documents business context behind data/reporting behavior that looks like it might be a bug but isn't. Read this before "fixing" any of the below in the report generators or pipelines.

## 1. brand_1.com (Finland) was closed in 2026

`brand_1.com` is correctly **excluded** from weekly and monthly reporting, but correctly **still included** in YTD figures (it contributes real historical data for the part of the year it was active, and YTD totals/comparisons should reflect that).

This is already implemented as intended:
- `src/generators/weekly_report_generator.py` and `src/generators/monthly_report_generator.py` explicitly filter out `domain == 'brand_1.com'` from both `df_brands` and `df_players` before any aggregation.
- `src/generators/ytd_report_generator.py` deliberately keeps `brand_1.com` in `df_brands`/`df_players` for the Snapshot table, Finland country totals, and Overall totals — it's only excluded from getting its own standalone brand summary card (see the `brands_in_country` filter that drops `brand_1.com` from per-brand cards while it stays in country/overall rollups).

**Do not** add `brand_1.com` back into weekly/monthly, and do not remove it from YTD aggregates. If YTD numbers look inflated relative to weekly/monthly run-rate for Finland, this closed brand's historical contribution is a likely reason to check first.

## 2. Lithuania (brand_1.lt, brand_5.lt) never has bonuses

Lithuanian gambling regulation never permits bonuses for these brands. Any bonus-related KPI (Bonus Cost, Bonus %, Bonus vs Budget, Bonus vs 12W/3M Avg, Bonus YoY) for Lithuania should always read **0 / N/A / a dash** — this is expected regulatory reality, not missing or broken data.

This is already handled explicitly in all three report generators: each has a `if country == 'Lithuania':` branch that renders a placeholder ("-") bonus row instead of computing a variance, and `generate_summary_card_content()` is called with `hide_bonus=True` for Lithuania's country and brand summary cards.

**Do not** treat a Lithuania bonus figure of 0 as a data pull failure, and do not remove the Lithuania bonus-hiding logic — it's intentional, not a workaround for a missing table.

**If a Lithuania bonus figure is non-zero (including negative), it is a data artifact, not real bonus activity.** The known mechanism: a sportsbook bet is occasionally resulted incorrectly, the player withdraws before it's corrected, and the correction/reversal flows through the pipeline tagged as a "bonus" line item. Treat any nonzero `bonus_cost` for `brand_1.lt`/`brand_5.lt` as noise from this kind of correction — don't read it as a trend, don't compute a YoY/% change on it, and don't build a narrative around it (e.g. a small negative HV bonus figure isn't "bonus efficiency," it's this artifact).

## 3. Sweden (brand_6.se) is a "passive" brand

Only one welcome bonus was ever regulatorily permitted for this brand, and the company has since stopped offering even that. There is no ongoing feature or marketing investment in this brand — it is intentionally in maintenance/passive status, not being actively grown.

**When analyzing this brand:** declining or flat Actives/GGR/NGR for `brand_6.se` (`Brand_6se_Sweden` domain) should be interpreted as the expected consequence of zero marketing spend and no bonus lever, not flagged as an unexplained problem or investigated as a churn/product issue. Variance-vs-budget or YoY drops here don't warrant the same root-cause digging that an actively-invested brand's decline would.

There is no special-case code for this in the generators today (Sweden is treated like any other country/brand in the KPI tables) — this is purely interpretive context for whoever is reading or writing the narrative/analysis around Sweden's numbers, not a data-handling rule.
