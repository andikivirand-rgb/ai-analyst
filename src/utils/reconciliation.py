import os
import random
from datetime import datetime, date

import pandas as pd

from src.utils.db_connections import get_ch_client
from src.utils.provenance import logged_ch_query

# Only these 7 currently-active brand_1-family domains have a validated,
# near-exact ClickHouse correspondence (0.00-0.64% diff, backtested against
# transactions_npl_casino). Brand_6/Brand_7 brands are sourced from
# Snowflake in the pull scripts and only have ClickHouse atomic-table data as
# a cross-check, which carries a known, unresolved ~1-10% digital/live
# classification variance (see CLAUDE.md's config/standards/ "Data sources"
# notes) -- too noisy for a 1% tolerance, so they're excluded from the
# automatic sample pool for now. brand_1.com (closed, 8th brand_1-family
# domain) is handled separately below via CLOSED_YTD_DOMAINS -- it was not
# part of this backtest, so don't assume its correspondence is equally tight.
BRAND_SIZE_TIERS = {
    'large': ['brand_1.lv'],
    'medium': ['brand_3.lv', 'brand_2.lv'],
    'small': ['brand_1.ee', 'brand_1.lt', 'brand_4.ee', 'brand_5.lt'],
}
ELIGIBLE_DOMAINS = {d for tier in BRAND_SIZE_TIERS.values() for d in tier}

# Closed brands: no ongoing activity for the RECENT_MONTHS_WINDOW/
# ACTIVES_RECENT_DAYS_WINDOW logic above to sample -- that logic would just
# find nothing recent and skip, which is not the same as confirming the
# domain's numbers are correct. Deliberately excluded from ELIGIBLE_DOMAINS
# (weekly/monthly pulls correctly drop this domain entirely -- see
# config/business_rules.md), but its historical data still feeds YTD
# aggregates, so use reconcile_closed_domain_ytd() below to validate it
# against its own actual active window instead.
CLOSED_YTD_DOMAINS = ['brand_1.com']

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(_REPO_ROOT, 'data', 'logs', 'reconciliation_log.csv')

TOLERANCE_PCT = 1.0

# Only sample from the N most recent fully-covered months. Postgres-vs-
# ClickHouse casino data has a real, pre-existing drift that grows for older
# periods (2-24% seen ~a year back) -- unrelated to the double-counting bug
# this check exists to catch -- so sampling further back would make the
# check fail on old, already-known noise instead of on new regressions.
RECENT_MONTHS_WINDOW = 3

LOG_COLUMNS = [
    'timestamp', 'script', 'domain', 'product', 'month',
    'pulled_turnover', 'ch_turnover', 'turnover_diff_pct',
    'pulled_ggr', 'ch_ggr', 'ggr_diff_pct', 'status', 'note',
]

ACTIVES_LOG_PATH = os.path.join(_REPO_ROOT, 'data', 'logs', 'actives_reconciliation_log.csv')

# Unvalidated tolerance -- see reconcile_actives_pull()'s docstring. The 1.0%
# TOLERANCE_PCT above was only adopted after backtesting turnover/GGR
# agreement; distinct-player-count agreement has no equivalent backtest yet,
# so this check runs log-only rather than blocking.
ACTIVES_TOLERANCE_PCT = 5.0

# Sample from the N most recent days that have data present. Day-level, not
# month-level, because summing daily distinct-player counts across a month
# does not equal that month's true unique actives (see CLAUDE.md's "two
# different actives metrics" note) -- aggregating to month grain first would
# compare two different metrics, not the same metric from two sources.
ACTIVES_RECENT_DAYS_WINDOW = 30

ACTIVES_LOG_COLUMNS = [
    'timestamp', 'script', 'domain', 'product', 'date',
    'pulled_actives', 'ch_actives', 'actives_diff_pct', 'status', 'note',
]


def _month_bounds(month):
    start = date(month.year, month.month, 1)
    end = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    return start, end


def _pct_diff(pulled, reference):
    if reference == 0:
        return 0.0 if pulled == 0 else float('inf')
    return (pulled - reference) / reference * 100.0


def _eligible_months(date_series, month_series):
    """Candidate months: fully covered by the pull's date range, and not the
    current (still-accumulating) calendar month.

    Coverage must be checked day-by-day, not just via min()/max() bounds --
    a rolling-window cache (e.g. a 12-week window plus a disjoint 1-week YoY
    chunk a year back) can have data_min/data_max bracket a month while a
    chunk in the middle of that month is actually missing. Bounds-only
    checking mistook such a gap for full coverage and compared a partial
    month's sum against ClickHouse's full-month total (see correction_log.md).
    """
    data_min = date_series.min()
    data_max = date_series.max()
    day_level = not (date_series.dt.day == 1).all()
    today = pd.Timestamp(datetime.now().date())
    current_month_start = pd.Timestamp(date(today.year, today.month, 1))
    present_dates = set(date_series.dt.normalize())

    covered = []
    for m in sorted(month_series.unique()):
        m = pd.Timestamp(m)
        if m == current_month_start:
            continue
        start, end = _month_bounds(m)
        if pd.Timestamp(start) < data_min:
            continue
        if day_level:
            if (pd.Timestamp(end) - pd.Timedelta(days=1)) > data_max:
                continue
            month_days = pd.date_range(start, pd.Timestamp(end) - pd.Timedelta(days=1))
            if not all(d in present_dates for d in month_days):
                continue
        covered.append(m)
    return covered


def _ch_casino_totals(client, domain, product, month_start, month_end, script_name='reconciliation.py'):
    product_filter = "casino_live_casino = 'yes'" if product == 'live_casino' else "casino_live_casino = 'no'"
    q = f"""
    SELECT SUM(bet), SUM(output) - SUM(input)
    FROM BRAND_1.transactions_npl_casino
    WHERE user_domain = '{domain}'
      AND transaction_date >= '{month_start}' AND transaction_date < '{month_end}'
      AND product IN ('casino', 'live_games')
      AND {product_filter}
    """
    r = logged_ch_query(client, q, script_name=script_name,
                         note=f'reconcile_pull cross-check: {domain}/{product}/{month_start}-{month_end}')
    turnover, ggr = r.result_rows[0]
    return float(turnover or 0.0), float(ggr or 0.0)


def _log_rows(rows):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    out = pd.DataFrame(rows, columns=LOG_COLUMNS)
    header = not os.path.exists(LOG_PATH)
    out.to_csv(LOG_PATH, mode='a', header=header, index=False)


def _skip(script_name, note):
    _log_rows([{
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'script': script_name, 'domain': '', 'product': '', 'month': '',
        'pulled_turnover': None, 'ch_turnover': None, 'turnover_diff_pct': None,
        'pulled_ggr': None, 'ch_ggr': None, 'ggr_diff_pct': None,
        'status': 'SKIPPED', 'note': note,
    }])
    print(f"Reconciliation check for {script_name}: skipped -- {note}")


def reconcile_pull(df, script_name, domain_col='domain', product_col='product',
                    date_col='date', turnover_col='turnover', ggr_col='ggr',
                    tolerance_pct=TOLERANCE_PCT, random_seed=None):
    """
    Cross-checks the pulled dataframe against ClickHouse for a sample of
    brand-product-month cells: the most recent fully-covered month plus up to
    two random prior months, sampled only from the RECENT_MONTHS_WINDOW most
    recent fully-covered months (see note above -- older months carry a
    separate, unrelated drift), across at least four brands spanning the size
    range. Scope is digital_casino/live_casino only, and the sample pool is
    restricted to the 8 brand_1-family domains (see BRAND_SIZE_TIERS above)
    -- the only ones with a validated near-exact ClickHouse correspondence.

    Every run's sampled cells are appended to data/logs/reconciliation_log.csv
    (status SKIPPED/OK/FAIL) so drift is visible over time.

    Raises RuntimeError -- without writing any output -- if any sampled cell's
    turnover or GGR differs from ClickHouse by more than tolerance_pct.
    Call this BEFORE writing the pull's output file.
    """
    rng = random.Random(random_seed)

    work = df.copy()
    work = work[work[product_col].isin(['digital_casino', 'live_casino'])]
    if work.empty:
        _skip(script_name, 'no digital_casino/live_casino rows to check')
        return

    work['_domain_norm'] = work[domain_col].astype(str).str.lower().str.strip()
    work = work[work['_domain_norm'].isin(ELIGIBLE_DOMAINS)]
    if work.empty:
        _skip(script_name, 'no eligible (brand_1-family) domains present to check')
        return

    work['_date'] = pd.to_datetime(work[date_col])
    work['_month'] = work['_date'].values.astype('datetime64[M]')

    covered_months = _eligible_months(work['_date'], work['_month'])
    if not covered_months:
        _skip(script_name, 'no fully-covered calendar month in this pull window')
        return

    # Restrict to a recent window: Postgres-vs-ClickHouse casino data has a
    # known, unrelated drift that grows for older months (2-24% seen a year
    # back), which would make this check fail constantly on pre-existing,
    # already-flagged noise rather than on new bugs. Sampling only the recent
    # window keeps the check meaningful for the double-counting class of bug
    # it exists to catch (see the incident logged in correction_log.md).
    recent_months = covered_months[-RECENT_MONTHS_WINDOW:]

    most_recent = recent_months[-1]
    prior_pool = recent_months[:-1]
    sample_months = [most_recent] + rng.sample(prior_pool, min(2, len(prior_pool)))

    agg = work.groupby(['_domain_norm', product_col, '_month'])[[turnover_col, ggr_col]].sum().reset_index()

    present_domains = set(agg['_domain_norm'].unique())
    sample_domains = []
    for doms in BRAND_SIZE_TIERS.values():
        avail = [d for d in doms if d in present_domains]
        if avail:
            sample_domains.append(rng.choice(avail))
    pool = [d for d in present_domains if d not in sample_domains]
    while len(sample_domains) < 4 and pool:
        pick = rng.choice(pool)
        sample_domains.append(pick)
        pool.remove(pick)

    if not sample_domains:
        _skip(script_name, 'no eligible domains present after tiering')
        return

    client = get_ch_client()
    results = []
    try:
        for month in sample_months:
            month_start, month_end = _month_bounds(month)
            for domain in sample_domains:
                for product in ('digital_casino', 'live_casino'):
                    row = agg[
                        (agg['_domain_norm'] == domain)
                        & (agg[product_col] == product)
                        & (agg['_month'] == month)
                    ]
                    pulled_turnover = float(row[turnover_col].sum()) if not row.empty else 0.0
                    pulled_ggr = float(row[ggr_col].sum()) if not row.empty else 0.0
                    ch_turnover, ch_ggr = _ch_casino_totals(client, domain, product, month_start, month_end, script_name=script_name)

                    turnover_diff_pct = _pct_diff(pulled_turnover, ch_turnover)
                    ggr_diff_pct = _pct_diff(pulled_ggr, ch_ggr)
                    status = 'FAIL' if (abs(turnover_diff_pct) > tolerance_pct or abs(ggr_diff_pct) > tolerance_pct) else 'OK'

                    results.append({
                        'timestamp': datetime.now().isoformat(timespec='seconds'),
                        'script': script_name,
                        'domain': domain,
                        'product': product,
                        'month': month.strftime('%Y-%m'),
                        'pulled_turnover': pulled_turnover,
                        'ch_turnover': ch_turnover,
                        'turnover_diff_pct': turnover_diff_pct,
                        'pulled_ggr': pulled_ggr,
                        'ch_ggr': ch_ggr,
                        'ggr_diff_pct': ggr_diff_pct,
                        'status': status,
                        'note': '',
                    })
    finally:
        client.close()

    _log_rows(results)

    failures = [r for r in results if r['status'] == 'FAIL']
    if failures:
        lines = [
            f"Reconciliation check FAILED for {script_name} -- output NOT written. "
            f"{len(failures)}/{len(results)} sampled cells differ from ClickHouse by more than {tolerance_pct}%:"
        ]
        for f in failures:
            lines.append(
                f"  {f['domain']} / {f['product']} / {f['month']}: "
                f"turnover pulled={f['pulled_turnover']:,.2f} vs ch={f['ch_turnover']:,.2f} ({f['turnover_diff_pct']:+.2f}%), "
                f"ggr pulled={f['pulled_ggr']:,.2f} vs ch={f['ch_ggr']:,.2f} ({f['ggr_diff_pct']:+.2f}%)"
            )
        message = "\n".join(lines)
        print(message)
        raise RuntimeError(message)

    print(f"Reconciliation check for {script_name}: OK ({len(results)} cells sampled, all within {tolerance_pct}%)")


def _ch_casino_actives(client, domain, product, day_str, script_name='reconciliation.py'):
    product_filter = "casino_live_casino = 'yes'" if product == 'live_casino' else "casino_live_casino = 'no'"
    q = f"""
    SELECT COUNT(DISTINCT user_external_id)
    FROM BRAND_1.transactions_npl_casino
    WHERE user_domain = '{domain}'
      AND transaction_date = '{day_str}'
      AND product IN ('casino', 'live_games')
      AND {product_filter}
    """
    r = logged_ch_query(client, q, script_name=script_name,
                         note=f'reconcile_actives_pull cross-check: {domain}/{product}/{day_str}')
    return int(r.result_rows[0][0] or 0)


def _log_actives_rows(rows):
    os.makedirs(os.path.dirname(ACTIVES_LOG_PATH), exist_ok=True)
    out = pd.DataFrame(rows, columns=ACTIVES_LOG_COLUMNS)
    header = not os.path.exists(ACTIVES_LOG_PATH)
    out.to_csv(ACTIVES_LOG_PATH, mode='a', header=header, index=False)


def reconcile_actives_pull(df, script_name, domain_col='domain', product_col='product',
                            date_col='date', actives_col='actives',
                            tolerance_pct=ACTIVES_TOLERANCE_PCT, random_seed=None):
    """
    Cross-checks pulled digital_casino/live_casino distinct-player actives
    counts for a sample of domain-product-day cells against a live
    COUNT(DISTINCT user_external_id) query on ClickHouse's
    transactions_npl_casino -- the same reference table reconcile_pull() uses
    for turnover/GGR, restricted to the same ELIGIBLE_DOMAINS.

    Unlike reconcile_pull(), this is LOG-ONLY: it never raises and never
    blocks a pull from writing its output. Postgres-vs-ClickHouse agreement
    for turnover/GGR was only promoted to a blocking check after being
    backtested to a known, tight tolerance (see BRAND_SIZE_TIERS above); no
    equivalent backtest exists yet for distinct-player-count agreement, so a
    mismatch here is logged to data/logs/actives_reconciliation_log.csv for
    review rather than treated as a proven bug. Revisit once enough log
    history validates a safe tolerance -- and only then consider making it
    block, mirroring how reconcile_pull() was hardened.

    Every run's sampled cells are appended to
    data/logs/actives_reconciliation_log.csv (status OK/WARN/SKIPPED).
    """
    rng = random.Random(random_seed)

    work = df.copy()
    work = work[work[product_col].isin(['digital_casino', 'live_casino'])]
    if work.empty:
        print(f"Actives reconciliation check for {script_name}: skipped -- no digital_casino/live_casino rows to check")
        return

    work['_domain_norm'] = work[domain_col].astype(str).str.lower().str.strip()
    work = work[work['_domain_norm'].isin(ELIGIBLE_DOMAINS)]
    if work.empty:
        print(f"Actives reconciliation check for {script_name}: skipped -- no eligible (brand_1-family) domains present")
        return

    work['_date'] = pd.to_datetime(work[date_col]).dt.normalize()
    today = pd.Timestamp(datetime.now().date())
    eligible_dates = sorted(d for d in work['_date'].unique() if pd.Timestamp(d) < today)
    if not eligible_dates:
        print(f"Actives reconciliation check for {script_name}: skipped -- no eligible (non-current) date in this pull")
        return

    recent_dates = eligible_dates[-ACTIVES_RECENT_DAYS_WINDOW:]
    most_recent = recent_dates[-1]
    prior_pool = recent_dates[:-1]
    sample_dates = [most_recent] + rng.sample(prior_pool, min(2, len(prior_pool)))

    agg = work.groupby(['_domain_norm', product_col, '_date'])[actives_col].sum().reset_index()

    present_domains = set(agg['_domain_norm'].unique())
    sample_domains = []
    for doms in BRAND_SIZE_TIERS.values():
        avail = [d for d in doms if d in present_domains]
        if avail:
            sample_domains.append(rng.choice(avail))
    pool = [d for d in present_domains if d not in sample_domains]
    while len(sample_domains) < 4 and pool:
        pick = rng.choice(pool)
        sample_domains.append(pick)
        pool.remove(pick)

    if not sample_domains:
        print(f"Actives reconciliation check for {script_name}: skipped -- no eligible domains present after tiering")
        return

    client = get_ch_client()
    results = []
    try:
        for day in sample_dates:
            day_str = pd.Timestamp(day).strftime('%Y-%m-%d')
            for domain in sample_domains:
                for product in ('digital_casino', 'live_casino'):
                    row = agg[
                        (agg['_domain_norm'] == domain)
                        & (agg[product_col] == product)
                        & (agg['_date'] == day)
                    ]
                    pulled_actives = float(row[actives_col].sum()) if not row.empty else 0.0
                    ch_actives = float(_ch_casino_actives(client, domain, product, day_str, script_name=script_name))
                    diff_pct = _pct_diff(pulled_actives, ch_actives)
                    status = 'WARN' if abs(diff_pct) > tolerance_pct else 'OK'

                    results.append({
                        'timestamp': datetime.now().isoformat(timespec='seconds'),
                        'script': script_name,
                        'domain': domain,
                        'product': product,
                        'date': day_str,
                        'pulled_actives': pulled_actives,
                        'ch_actives': ch_actives,
                        'actives_diff_pct': diff_pct,
                        'status': status,
                        'note': '',
                    })
    finally:
        client.close()

    _log_actives_rows(results)

    warned = [r for r in results if r['status'] == 'WARN']
    if warned:
        lines = [
            f"Actives reconciliation check for {script_name}: {len(warned)}/{len(results)} sampled cells "
            f"differ from ClickHouse by more than {tolerance_pct}% (logged only, pipeline NOT blocked):"
        ]
        for w in warned:
            lines.append(
                f"  {w['domain']} / {w['product']} / {w['date']}: "
                f"pulled={w['pulled_actives']:.0f} vs ch={w['ch_actives']:.0f} ({w['actives_diff_pct']:+.2f}%)"
            )
        print("\n".join(lines))
    else:
        print(f"Actives reconciliation check for {script_name}: OK ({len(results)} cells sampled, all within {tolerance_pct}%)")


def reconcile_closed_domain_ytd(df, script_name, domain, domain_col='domain', product_col='product',
                                 date_col='date', turnover_col='turnover', ggr_col='ggr',
                                 tolerance_pct=TOLERANCE_PCT, n_months=3, random_seed=None):
    """
    Validates a closed brand's (e.g. brand_1.com) historical turnover/GGR
    against ClickHouse -- for domains that still feed YTD/yearly aggregates
    (see config/business_rules.md) but have no ongoing activity for
    reconcile_pull()'s RECENT_MONTHS_WINDOW logic to sample.

    Unlike reconcile_pull()/reconcile_actives_pull(), this is NOT meant to
    run on every pipeline invocation: a closed domain's history doesn't
    change, so there's no ongoing regression to guard against -- once
    confirmed, it stays confirmed. Call it manually when validating a YTD
    figure that includes this domain, e.g.:

        from src.utils.reconciliation import reconcile_closed_domain_ytd
        import pandas as pd
        df = pd.read_csv('data/raw/brand_daily_kpis_comparison.csv')
        reconcile_closed_domain_ytd(df, script_name='manual_check', domain='brand_1.com')

    Samples up to n_months from across the domain's ACTUAL active history --
    determined from what's present in df, not an assumed closure date, since
    the exact closure date isn't hardcoded anywhere else in this codebase
    either (see config/standards/outlier_and_margin_variance_standard.md,
    which only says "closed in 2026"). RAISES on a >tolerance_pct diff, same
    as reconcile_pull() -- a positive confirmation is the entire point of
    running this.

    Caveat: tolerance_pct defaults to the same 1.0% used for the 7 backtested
    ELIGIBLE_DOMAINS above, but brand_1.com was not part of that backtest --
    treat a first-run FAIL as "needs investigation," not automatic proof of
    a pipeline bug, until this domain's own correspondence is characterized.
    """
    rng = random.Random(random_seed)

    work = df.copy()
    work = work[work[product_col].isin(['digital_casino', 'live_casino'])]
    work['_domain_norm'] = work[domain_col].astype(str).str.lower().str.strip()
    work = work[work['_domain_norm'] == domain.lower()]
    if work.empty:
        print(f"Closed-domain YTD reconciliation for {script_name}/{domain}: skipped -- no digital_casino/live_casino rows for this domain")
        return

    work['_date'] = pd.to_datetime(work[date_col])
    work['_month'] = work['_date'].values.astype('datetime64[M]')

    covered_months = _eligible_months(work['_date'], work['_month'])
    if not covered_months:
        print(f"Closed-domain YTD reconciliation for {script_name}/{domain}: skipped -- no fully-covered calendar month found")
        return

    sample_months = covered_months if len(covered_months) <= n_months else rng.sample(covered_months, n_months)

    agg = work.groupby([product_col, '_month'])[[turnover_col, ggr_col]].sum().reset_index()

    client = get_ch_client()
    results = []
    try:
        for month in sample_months:
            month_start, month_end = _month_bounds(month)
            for product in ('digital_casino', 'live_casino'):
                row = agg[(agg[product_col] == product) & (agg['_month'] == month)]
                pulled_turnover = float(row[turnover_col].sum()) if not row.empty else 0.0
                pulled_ggr = float(row[ggr_col].sum()) if not row.empty else 0.0
                ch_turnover, ch_ggr = _ch_casino_totals(client, domain, product, month_start, month_end, script_name=script_name)

                turnover_diff_pct = _pct_diff(pulled_turnover, ch_turnover)
                ggr_diff_pct = _pct_diff(pulled_ggr, ch_ggr)
                status = 'FAIL' if (abs(turnover_diff_pct) > tolerance_pct or abs(ggr_diff_pct) > tolerance_pct) else 'OK'

                results.append({
                    'timestamp': datetime.now().isoformat(timespec='seconds'),
                    'script': script_name,
                    'domain': domain,
                    'product': product,
                    'month': month.strftime('%Y-%m'),
                    'pulled_turnover': pulled_turnover,
                    'ch_turnover': ch_turnover,
                    'turnover_diff_pct': turnover_diff_pct,
                    'pulled_ggr': pulled_ggr,
                    'ch_ggr': ch_ggr,
                    'ggr_diff_pct': ggr_diff_pct,
                    'status': status,
                    'note': 'closed_domain_ytd_manual_check',
                })
    finally:
        client.close()

    _log_rows(results)

    failures = [r for r in results if r['status'] == 'FAIL']
    if failures:
        lines = [
            f"Closed-domain YTD reconciliation FAILED for {script_name}/{domain}. "
            f"{len(failures)}/{len(results)} sampled cells differ from ClickHouse by more than {tolerance_pct}%:"
        ]
        for f in failures:
            lines.append(
                f"  {f['product']} / {f['month']}: "
                f"turnover pulled={f['pulled_turnover']:,.2f} vs ch={f['ch_turnover']:,.2f} ({f['turnover_diff_pct']:+.2f}%), "
                f"ggr pulled={f['pulled_ggr']:,.2f} vs ch={f['ch_ggr']:,.2f} ({f['ggr_diff_pct']:+.2f}%)"
            )
        message = "\n".join(lines)
        print(message)
        raise RuntimeError(message)

    print(f"Closed-domain YTD reconciliation for {script_name}/{domain}: OK ({len(results)} cells sampled across {len(sample_months)} historical months, all within {tolerance_pct}%)")
