import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
import warnings

# Windows consoles default to cp1252, which can't encode the emoji in status
# prints below - force utf-8 so a cosmetic print never crashes a successful run.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings('ignore')

# ==========================================
# 1. HELPER FUNCTIONS, MAPPINGS & ORDERING
# ==========================================
COUNTRY_ORDER = ['Latvia', 'Estonia', 'Lithuania', 'Finland', 'Sweden', 'Other']
BRAND_ORDER = [
    'brand_1.lv', 'brand_2.lv', 'brand_3.lv',
    'brand_1.ee', 'brand_4.ee', 'Brand_6_Estonia', 'Brand_7_Estonia',
    'brand_1.lt', 'brand_5.lt',
    'Brand_6_Finland', 'Brand_7_Finland',
    'Brand_6se_Sweden'
]

def get_country(d):
    if pd.isna(d): return 'Other'
    d_lower = str(d).lower()
    
    if '.ee' in d_lower or 'estonia' in d_lower or 'brand_4' in d_lower: return 'Estonia'
    if '.lv' in d_lower or 'brand_2' in d_lower or 'brand_3' in d_lower or 'latvia' in d_lower: return 'Latvia'
    if '.lt' in d_lower or 'lithuania' in d_lower: return 'Lithuania'
    if 'finland' in d_lower: return 'Finland'
    if 'sweden' in d_lower or '.se' in d_lower: return 'Sweden'
    return 'Other'

def get_brand_color(domain):
    d = str(domain).lower()
    if 'brand_1.lv' in d: return '#F21D3A'
    if 'brand_2' in d: return '#7355BC'
    if 'brand_3' in d: return '#2763CF'
    if 'brand_1.ee' in d or ('brand_1' in d and 'estonia' in d): return '#2C3E3E'
    if 'brand_4' in d: return '#FF5087'
    if 'brand_6' in d and ('estonia' in d or 'ee' in d): return '#1A7560'
    if 'boost' in d and ('estonia' in d or 'ee' in d): return '#FDC03D'
    if 'brand_1.lt' in d: return '#FFF61A'
    if 'brand_5' in d: return '#E88181'
    if 'brand_6' in d and ('finland' in d or 'fi' in d): return '#545F6C'
    if 'boost' in d and ('finland' in d or 'fi' in d): return '#4F1ABE'
    if 'brand_6' in d and ('sweden' in d or 'se' in d): return '#FC5822'
    return '#0984e3'

def calc_var(curr, prev):
    if pd.isna(prev) or pd.isna(curr):
        return np.nan
    if prev == 0:
        return 1.0 if curr > 0 else (-1.0 if curr < 0 else 0.0)
    return (curr - prev) / abs(prev)

def format_var(variance, is_cost=False, is_overall=False, delta=None, is_currency=False):
    if pd.isna(variance) or np.isinf(variance):
        return "<span>N/A</span>"
    
    val = abs(variance)
    direction = "up" if variance > 0 else "down" if variance < 0 else "flat"
    color_class = "positive" if (variance > 0 and not is_cost) or (variance < 0 and is_cost) else "negative"
    if variance == 0: color_class = ""
    
    delta_str = ""
    if delta is not None and not pd.isna(delta):
        abs_delta = abs(delta)
        sign = "+" if delta > 0 else ("-" if delta < 0 else "")
        if is_currency:
            delta_str = f" ({sign}&euro;{abs_delta:,.0f})"
        else:
            delta_str = f" ({sign}{abs_delta:,.0f})"
            
    return f'<span class="{color_class}">{direction} {val:.0%}{delta_str}</span>'

def format_budget_cell(variance, variance_formatted_text, is_cost=False):
    if pd.isna(variance) or np.isinf(variance) or variance == 0:
        bar_html = "<div class='bar-wrapper'><div class='bar-left'></div><div class='bar-right'></div></div>"
    else:
        max_variance_cap = 0.50
        val = min((abs(variance) / max_variance_cap) * 100, 100)
        
        if variance < 0:
            color_class = "fill-green" if is_cost else "fill-red"
            bar_html = f"<div class='bar-wrapper'><div class='bar-left'><div class='{color_class}' style='width: {val}%;'></div></div><div class='bar-right'></div></div>"
        else:
            color_class = "fill-red" if is_cost else "fill-green"
            bar_html = f"<div class='bar-wrapper'><div class='bar-left'></div><div class='bar-right'><div class='{color_class}' style='width: {val}%;'></div></div></div>"
            
    return f"<td><div class='budget-cell'>{bar_html}<div style='min-width: 140px; text-align: right;'>{variance_formatted_text}</div></div></td>"

def get_extreme_outliers(df_cw: pd.DataFrame, min_impact_eur: float = 1000) -> pd.DataFrame:
    if df_cw.empty: return pd.DataFrame()
    player_totals = df_cw.groupby(['domain', 'external_id']).agg({'ngr': 'sum'}).reset_index()
    outlier_list = []
    for brand in player_totals['domain'].unique():
        brand_data = player_totals[player_totals['domain'] == brand]
        Q1, Q3 = brand_data['ngr'].quantile(0.25), brand_data['ngr'].quantile(0.75)
        IQR = Q3 - Q1
        lower_bound, upper_bound = Q1 - 3.0 * IQR, Q3 + 3.0 * IQR
        
        outliers = brand_data[
            ((brand_data['ngr'] < lower_bound) & (brand_data['ngr'] <= -min_impact_eur)) | 
            ((brand_data['ngr'] > upper_bound) & (brand_data['ngr'] >= min_impact_eur))
        ].copy()
        
        if not outliers.empty:
            outliers['impact_type'] = 'Positive (Player Lost)'
            outliers.loc[outliers['ngr'] < lower_bound, 'impact_type'] = 'Negative (Player Won)'
            outlier_list.append(outliers)
            
    if not outlier_list: return pd.DataFrame()
    all_outliers = pd.concat(outlier_list, ignore_index=True)
    all_outliers['abs_impact'] = all_outliers['ngr'].abs()
    return all_outliers.sort_values(by=['domain', 'abs_impact'], ascending=[True, False]).drop(columns=['abs_impact'])

# ==========================================
# 2. LOAD & SLICE DATA
# ==========================================
print("Loading data files...")
try:
    # Update paths to point to the new data/raw/ directory
    df_brands = pd.read_csv('data/raw/brand_daily_kpis_comparison.csv')
    df_players = pd.read_csv('data/raw/player_activity_master.csv')
except FileNotFoundError as e:
    print(f"Error: Could not find required CSV files. ({e})")
    exit()

for col in df_brands.columns:
    if col.startswith('budget_') and col.endswith('_x'):
        df_brands.rename(columns={col: col.replace('_x', '')}, inplace=True)

df_brands['date'] = pd.to_datetime(df_brands['date'])
df_players['date'] = pd.to_datetime(df_players['date'])

df_brands = df_brands[df_brands['domain'] != 'brand_1.com']
df_players = df_players[df_players['domain'] != 'brand_1.com']

df_players['external_id'] = df_players['external_id'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
for col in ['event_type', 'type']:
    if col in df_players.columns:
        df_players[col] = df_players[col].fillna('')

df_players = df_players.drop_duplicates(subset=['date', 'domain', 'external_id', 'product', 'event_type', 'type'], keep='last')

df_brands['Country'] = df_brands['domain'].apply(get_country)
df_players['Country'] = df_players['domain'].apply(get_country)

max_data_date = df_brands['date'].max()
if max_data_date.weekday() == 6: 
    current_week_start = max_data_date - pd.Timedelta(days=6)
else: 
    current_week_start = max_data_date - pd.Timedelta(days=max_data_date.weekday() + 7)

current_week_end = current_week_start + pd.Timedelta(days=6)
title_date_str = f"{current_week_start.strftime('%B %d')} - {current_week_end.strftime('%B %d, %Y')}"

# Exact YoY 1-Week Date Range (364 Days Shift)
yoy_cw_start = current_week_start - pd.Timedelta(days=364)
yoy_cw_end = current_week_end - pd.Timedelta(days=364)

df_b_cw = df_brands[(df_brands['date'] >= current_week_start) & (df_brands['date'] <= current_week_end)]
df_b_12w = df_brands[(df_brands['date'] < current_week_start) & (df_brands['date'] >= current_week_start - pd.Timedelta(days=84))]
df_p_cw = df_players[(df_players['date'] >= current_week_start) & (df_players['date'] <= current_week_end)]
df_p_12w = df_players[(df_players['date'] < current_week_start) & (df_players['date'] >= current_week_start - pd.Timedelta(days=84))]
df_p_yoy = df_players[(df_players['date'] >= yoy_cw_start) & (df_players['date'] <= yoy_cw_end)]

cw_active_mask = (df_p_cw['turnover'] > 0) | ((df_p_cw['product'] == 'poker') & (df_p_cw['ggr'] != 0))
p12_active_mask = (df_p_12w['turnover'] > 0) | ((df_p_12w['product'] == 'poker') & (df_p_12w['ggr'] != 0))
yoy_active_mask = (df_p_yoy['turnover'] > 0) | ((df_p_yoy['product'] == 'poker') & (df_p_yoy['ggr'] != 0))

outliers_df = get_extreme_outliers(df_p_cw[cw_active_mask])

cw_actives_series = df_p_cw[cw_active_mask].groupby('domain')['external_id'].nunique() if not df_p_cw.empty else pd.Series()
df_p_12w['week_block'] = ((current_week_start - df_p_12w['date']).dt.days - 1) // 7
p12_actives_avg_series = df_p_12w[p12_active_mask].groupby(['domain', 'week_block'])['external_id'].nunique().groupby('domain').mean() if not df_p_12w.empty else pd.Series()

# ==========================================
# 3. BUILD MATRIX DATA
# ==========================================
agg_df = df_b_cw.groupby('domain').agg({
    'nadps': 'sum', 'regs': 'sum', 'turnover': 'sum', 
    'ggr': 'sum', 'bonus': 'sum', 'ngr': 'sum', 'budget_ngr': 'sum',
    'budget_bonus': 'sum'
}).reset_index()

agg_df['actives'] = agg_df['domain'].map(cw_actives_series).fillna(0)

prod_ggr = df_b_cw.pivot_table(index='domain', columns='product', values='ggr', aggfunc='sum').fillna(0).reset_index()
matrix = pd.merge(agg_df, prod_ggr, on='domain')
matrix['Country'] = matrix['domain'].apply(get_country)

for prod in ['digital_casino', 'live_casino', 'sb', 'poker']:
    if prod not in matrix.columns:
        matrix[prod] = 0.0

final_matrix_rows = []
valid_countries = [c for c in COUNTRY_ORDER if c in matrix['Country'].unique()]

for country in valid_countries:
    c_data = matrix[matrix['Country'] == country].copy()
    c_data['domain'] = pd.Categorical(c_data['domain'], categories=BRAND_ORDER, ordered=True)
    c_data = c_data.sort_values('domain')
    
    c_data['Conversion Rate'] = np.where(c_data['regs'] > 0, c_data['nadps'] / c_data['regs'], 0)
    c_data['GGR %'] = np.where(c_data['turnover'] > 0, c_data['ggr'] / c_data['turnover'], 0)
    c_data['Bonus %'] = np.where(c_data['ggr'] > 0, c_data['bonus'] / c_data['ggr'], 0)
    c_data['NGR vs Budget'] = np.where(c_data['budget_ngr'] > 0, (c_data['ngr'] / c_data['budget_ngr']) - 1, 0)
    
    c_totals = c_data[['nadps', 'regs', 'turnover', 'ggr', 'digital_casino', 'live_casino', 'sb', 'poker', 'bonus', 'ngr', 'budget_ngr', 'budget_bonus']].sum(numeric_only=True)
    true_country_actives = df_p_cw[(df_p_cw['Country'] == country) & cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0
    
    c_total_dict = c_totals.to_dict()
    c_total_dict.update({
        'domain': f"{country} Total", 'Country': country, 'is_total': True,
        'actives': true_country_actives,
        'Conversion Rate': c_totals['nadps'] / c_totals['regs'] if c_totals['regs'] > 0 else 0,
        'GGR %': c_totals['ggr'] / c_totals['turnover'] if c_totals['turnover'] > 0 else 0,
        'Bonus %': c_totals['bonus'] / c_totals['ggr'] if c_totals['ggr'] > 0 else 0,
        'NGR vs Budget': (c_totals['ngr'] / c_totals['budget_ngr']) - 1 if c_totals['budget_ngr'] > 0 else 0
    })
    
    for _, row in c_data.iterrows(): final_matrix_rows.append(row.to_dict())
    final_matrix_rows.append(c_total_dict)

overall_totals = matrix[['nadps', 'regs', 'turnover', 'ggr', 'digital_casino', 'live_casino', 'sb', 'poker', 'bonus', 'ngr', 'budget_ngr', 'budget_bonus']].sum(numeric_only=True)
true_overall_actives = df_p_cw[cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0

overall_dict = overall_totals.to_dict()
overall_dict.update({
    'domain': 'OVERALL TOTAL', 'Country': 'ALL', 'is_overall': True, 'is_total': True,
    'actives': true_overall_actives,
    'Conversion Rate': overall_totals['nadps'] / overall_totals['regs'] if overall_totals['regs'] > 0 else 0,
    'GGR %': overall_totals['ggr'] / overall_totals['turnover'] if overall_totals['turnover'] > 0 else 0,
    'Bonus %': overall_totals['bonus'] / overall_totals['ggr'] if overall_totals['ggr'] > 0 else 0,
    'NGR vs Budget': (overall_totals['ngr'] / overall_totals['budget_ngr']) - 1 if overall_totals['budget_ngr'] > 0 else 0
})
final_matrix_rows.append(overall_dict)

# ==========================================
# 4. GENERATE NEW COUNTRY-LEVEL TABLES
# ==========================================
product_tables_html = ""
for prod_key, prod_display in [('digital_casino', 'Digital Casino'), ('live_casino', 'Live Casino'), ('sb', 'Sportsbook'), ('poker', 'Poker')]:
    product_tables_html += f"<div><h2 class='sub-table-title'>{prod_display} Performance</h2>"
    product_tables_html += "<table class='matrix'><thead><tr><th class='text-left'>Country</th><th>NGR</th><th>vs Budget</th><th>vs 12W Avg</th><th>YoY</th></tr></thead><tbody>"
    
    tot_ngr, tot_budget, tot_p12, tot_yoy, tot_turnover = 0, 0, 0, 0, 0
    
    for country in valid_countries:
        c_cw = df_b_cw[df_b_cw['Country'] == country]
        c_p12 = df_b_12w[df_b_12w['Country'] == country]
        
        cw_p_ngr = round(c_cw[c_cw['product'] == prod_key]['ngr'].sum())
        if cw_p_ngr == 0: continue 
        
        cw_p_turnover = c_cw[c_cw['product'] == prod_key]['turnover'].sum()
        cw_p_budget = round(c_cw[c_cw['product'] == prod_key]['budget_ngr'].sum())
        p12_p_ngr = round(c_p12[c_p12['product'] == prod_key]['ngr'].sum() / 12)
        cw_yoy_p_ngr = round(c_cw[c_cw['product'] == prod_key]['ngr_yoy'].sum()) if 'ngr_yoy' in c_cw.columns else 0
        
        tot_ngr += cw_p_ngr; tot_budget += cw_p_budget; tot_p12 += p12_p_ngr; tot_yoy += cw_yoy_p_ngr; tot_turnover += cw_p_turnover
        
        ngr_pct = (cw_p_ngr / cw_p_turnover) if cw_p_turnover > 0 else 0
        var_p_bud = calc_var(cw_p_ngr, cw_p_budget)
        fmt_text = format_var(var_p_bud, delta=cw_p_ngr - cw_p_budget, is_currency=True)
        
        product_tables_html += f"<tr><td class='text-left'><strong>{country}</strong></td><td>&euro;{cw_p_ngr:,.0f} ({ngr_pct:.1%})</td>" \
                               f"{format_budget_cell(var_p_bud, fmt_text)}" \
                               f"<td>{format_var(calc_var(cw_p_ngr, p12_p_ngr), delta=cw_p_ngr - p12_p_ngr, is_currency=True)}</td>" \
                               f"<td>{format_var(calc_var(cw_p_ngr, cw_yoy_p_ngr), delta=cw_p_ngr - cw_yoy_p_ngr, is_currency=True)}</td></tr>"
    
    if tot_ngr != 0 or tot_budget != 0:
        var_tot_bud = calc_var(tot_ngr, tot_budget)
        fmt_tot_text = format_var(var_tot_bud, delta=tot_ngr - tot_budget, is_overall=True, is_currency=True)
        tot_ngr_pct = (tot_ngr / tot_turnover) if tot_turnover > 0 else 0
        
        product_tables_html += f"<tr class='total-row'><td class='text-left'>OVERALL TOTAL</td><td>&euro;{tot_ngr:,.0f} ({tot_ngr_pct:.1%})</td>" \
                               f"{format_budget_cell(var_tot_bud, fmt_tot_text)}" \
                               f"<td>{format_var(calc_var(tot_ngr, tot_p12), delta=tot_ngr - tot_p12, is_overall=True, is_currency=True)}</td>" \
                               f"<td>{format_var(calc_var(tot_ngr, tot_yoy), delta=tot_ngr - tot_yoy, is_overall=True, is_currency=True)}</td></tr>"
        
    product_tables_html += "</tbody></table></div>"

ngr_tbl = "<table class='matrix'><thead><tr><th class='text-left'>Country</th><th>NGR</th><th>vs Budget</th><th>vs 12W Avg</th><th>YoY</th></tr></thead><tbody>"
actives_tbl = "<table class='matrix'><thead><tr><th class='text-left'>Country</th><th>Actives</th><th>vs 12W Avg</th><th>YoY</th></tr></thead><tbody>"
nadp_tbl = "<table class='matrix'><thead><tr><th class='text-left'>Country</th><th>NADP</th><th>vs 12W Avg</th><th>YoY</th></tr></thead><tbody>"
bonus_tbl = "<table class='matrix'><thead><tr><th class='text-left'>Country</th><th>Bonus Cost</th><th>vs Budget</th><th>vs 12W Avg</th><th>YoY</th></tr></thead><tbody>"

tot_ngr, tot_ngr_bud, tot_ngr_p12, tot_ngr_yoy, tot_turnover = 0, 0, 0, 0, 0
tot_nadp, tot_nadp_p12, tot_nadp_yoy = 0, 0, 0
tot_bon, tot_bon_bud, tot_bon_p12, tot_bon_yoy, tot_ggr = 0, 0, 0, 0, 0

for country in valid_countries:
    c_cw = df_b_cw[df_b_cw['Country'] == country]
    c_p12 = df_b_12w[df_b_12w['Country'] == country]
    
    cw_ngr = round(c_cw['ngr'].sum())
    cw_turnover = c_cw['turnover'].sum()
    bud_ngr = round(c_cw['budget_ngr'].sum()) if 'budget_ngr' in c_cw.columns else 0
    p12_ngr = round(c_p12['ngr'].sum() / 12)
    cw_yoy_ngr = round(c_cw['ngr_yoy'].sum()) if 'ngr_yoy' in c_cw.columns else 0
    
    cw_nadp = round(c_cw['nadps'].sum())
    p12_nadp = round(c_p12['nadps'].sum() / 12)
    cw_yoy_nadp = round(c_cw['nadps_yoy'].sum()) if 'nadps_yoy' in c_cw.columns else 0
    
    cw_bon = round(c_cw['bonus'].sum())
    cw_ggr = c_cw['ggr'].sum()
    bud_bon = round(c_cw['budget_bonus'].sum()) if 'budget_bonus' in c_cw.columns else 0
    p12_bon = round(c_p12['bonus'].sum() / 12)
    cw_yoy_bon = round(c_cw['bonus_yoy'].sum()) if 'bonus_yoy' in c_cw.columns else 0
    
    # TRUE Unique Actives for CW, 12W, and YoY
    true_cw_act = df_p_cw[(df_p_cw['Country'] == country) & cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0
    p12_country_df = df_p_12w[(df_p_12w['Country'] == country) & p12_active_mask]
    true_p12_act = p12_country_df.groupby('week_block')['external_id'].nunique().mean() if not p12_country_df.empty else 0
    true_yoy_act = df_p_yoy[(df_p_yoy['Country'] == country) & yoy_active_mask]['external_id'].nunique() if not df_p_yoy.empty else 0
    
    tot_ngr += cw_ngr; tot_ngr_bud += bud_ngr; tot_ngr_p12 += p12_ngr; tot_ngr_yoy += cw_yoy_ngr; tot_turnover += cw_turnover
    tot_nadp += cw_nadp; tot_nadp_p12 += p12_nadp; tot_nadp_yoy += cw_yoy_nadp
    tot_bon += cw_bon; tot_bon_bud += bud_bon; tot_bon_p12 += p12_bon; tot_bon_yoy += cw_yoy_bon; tot_ggr += cw_ggr
    
    ngr_pct = (cw_ngr / cw_turnover) if cw_turnover > 0 else 0
    bon_pct = (cw_bon / cw_ggr) if cw_ggr > 0 else 0
    
    var_ngr_bud = calc_var(cw_ngr, bud_ngr)
    fmt_text_ngr = format_var(var_ngr_bud, delta=cw_ngr - bud_ngr, is_currency=True)
    
    ngr_tbl += f"<tr><td class='text-left'><strong>{country}</strong></td><td>&euro;{cw_ngr:,.0f} ({ngr_pct:.1%})</td>" \
               f"{format_budget_cell(var_ngr_bud, fmt_text_ngr)}" \
               f"<td>{format_var(calc_var(cw_ngr, p12_ngr), delta=cw_ngr - p12_ngr, is_currency=True)}</td>" \
               f"<td>{format_var(calc_var(cw_ngr, cw_yoy_ngr), delta=cw_ngr - cw_yoy_ngr, is_currency=True)}</td></tr>"
               
    actives_tbl += f"<tr><td class='text-left'><strong>{country}</strong></td><td>{true_cw_act:,.0f}</td>" \
                   f"<td>{format_var(calc_var(true_cw_act, true_p12_act), delta=true_cw_act - true_p12_act)}</td>" \
                   f"<td>{format_var(calc_var(true_cw_act, true_yoy_act), delta=true_cw_act - true_yoy_act)}</td></tr>"
                   
    nadp_tbl += f"<tr><td class='text-left'><strong>{country}</strong></td><td>{cw_nadp:,.0f}</td>" \
                f"<td>{format_var(calc_var(cw_nadp, p12_nadp), delta=cw_nadp - p12_nadp)}</td>" \
                f"<td>{format_var(calc_var(cw_nadp, cw_yoy_nadp), delta=cw_nadp - cw_yoy_nadp)}</td></tr>"
    
    if country == 'Lithuania':
        bonus_tbl += f"<tr><td class='text-left'><strong>{country}</strong></td><td>-</td><td><div class='budget-cell'><div class='bar-wrapper'><div class='bar-left'></div><div class='bar-right'></div></div><div style='width: 140px; text-align: right;'>-</div></div></td><td>-</td><td>-</td></tr>"
    else:
        var_bon_bud = calc_var(cw_bon, bud_bon)
        fmt_text_bon = format_var(var_bon_bud, delta=cw_bon - bud_bon, is_cost=True, is_currency=True)
        bonus_tbl += f"<tr><td class='text-left'><strong>{country}</strong></td><td>&euro;{cw_bon:,.0f} ({bon_pct:.1%})</td>" \
                     f"{format_budget_cell(var_bon_bud, fmt_text_bon, is_cost=True)}" \
                     f"<td>{format_var(calc_var(cw_bon, p12_bon), delta=cw_bon - p12_bon, is_cost=True, is_currency=True)}</td>" \
                     f"<td>{format_var(calc_var(cw_bon, cw_yoy_bon), delta=cw_bon - cw_yoy_bon, is_cost=True, is_currency=True)}</td></tr>"

true_overall_p12_act = df_p_12w[p12_active_mask].groupby('week_block')['external_id'].nunique().mean() if not df_p_12w.empty else 0
true_tot_cw_act = df_p_cw[cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0
true_tot_yoy_act = df_p_yoy[yoy_active_mask]['external_id'].nunique() if not df_p_yoy.empty else 0

var_tot_ngr_bud = calc_var(tot_ngr, tot_ngr_bud)
fmt_tot_ngr = format_var(var_tot_ngr_bud, delta=tot_ngr - tot_ngr_bud, is_overall=True, is_currency=True)
tot_ngr_pct = (tot_ngr / tot_turnover) if tot_turnover > 0 else 0

ngr_tbl += f"<tr class='total-row'><td class='text-left'>OVERALL TOTAL</td><td>&euro;{tot_ngr:,.0f} ({tot_ngr_pct:.1%})</td>" \
           f"{format_budget_cell(var_tot_ngr_bud, fmt_tot_ngr)}" \
           f"<td>{format_var(calc_var(tot_ngr, tot_ngr_p12), delta=tot_ngr - tot_ngr_p12, is_overall=True, is_currency=True)}</td>" \
           f"<td>{format_var(calc_var(tot_ngr, tot_ngr_yoy), delta=tot_ngr - tot_ngr_yoy, is_overall=True, is_currency=True)}</td></tr></tbody></table>"
           
actives_tbl += f"<tr class='total-row'><td class='text-left'>OVERALL TOTAL</td><td>{true_tot_cw_act:,.0f}</td>" \
               f"<td>{format_var(calc_var(true_tot_cw_act, true_overall_p12_act), delta=true_tot_cw_act - true_overall_p12_act, is_overall=True)}</td>" \
               f"<td>{format_var(calc_var(true_tot_cw_act, true_tot_yoy_act), delta=true_tot_cw_act - true_tot_yoy_act, is_overall=True)}</td></tr></tbody></table>"
               
nadp_tbl += f"<tr class='total-row'><td class='text-left'>OVERALL TOTAL</td><td>{tot_nadp:,.0f}</td>" \
            f"<td>{format_var(calc_var(tot_nadp, tot_nadp_p12), delta=tot_nadp - tot_nadp_p12, is_overall=True)}</td>" \
            f"<td>{format_var(calc_var(tot_nadp, tot_nadp_yoy), delta=tot_nadp - tot_nadp_yoy, is_overall=True)}</td></tr></tbody></table>"
            
var_tot_bon_bud = calc_var(tot_bon, tot_bon_bud)
fmt_tot_bon = format_var(var_tot_bon_bud, delta=tot_bon - tot_bon_bud, is_cost=True, is_overall=True, is_currency=True)
tot_bon_pct = (tot_bon / tot_ggr) if tot_ggr > 0 else 0

bonus_tbl += f"<tr class='total-row'><td class='text-left'>OVERALL TOTAL</td><td>&euro;{tot_bon:,.0f} ({tot_bon_pct:.1%})</td>" \
             f"{format_budget_cell(var_tot_bon_bud, fmt_tot_bon, is_cost=True)}" \
             f"<td>{format_var(calc_var(tot_bon, tot_bon_p12), delta=tot_bon - tot_bon_p12, is_cost=True, is_overall=True, is_currency=True)}</td>" \
             f"<td>{format_var(calc_var(tot_bon, tot_bon_yoy), delta=tot_bon - tot_bon_yoy, is_cost=True, is_overall=True, is_currency=True)}</td></tr></tbody></table>"

kpi_tables_html = f"""
<table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
    <tr>
        <td style="width: 55%; vertical-align: top; padding: 0;">
            <h2 class='sub-table-title'>NGR</h2>
            {ngr_tbl}
            <div style="height: 15px;"></div>
            <h2 class='sub-table-title'>Bonus Cost</h2>
            {bonus_tbl}
        </td>
        <td style="width: 3%; padding: 0;"></td>
        <td style="width: 42%; vertical-align: top; padding: 0;">
            <h2 class='sub-table-title'>Actives</h2>
            {actives_tbl}
            <div style="height: 15px;"></div>
            <h2 class='sub-table-title'>NADP</h2>
            {nadp_tbl}
        </td>
    </tr>
</table>
"""

# ==========================================
# 5. GENERATE TEXT SUMMARIES (Deep Dives)
# ==========================================
def generate_summary_card_content(cw_df, p12_df, true_cw_act, true_p12_act, true_yoy_act, hide_bonus=False):
    cw_data = cw_df.sum(numeric_only=True)
    p12_data = p12_df.sum(numeric_only=True)

    cw_ngr = round(cw_data.get('ngr', 0))
    cw_nadp = round(cw_data.get('nadps', 0))
    cw_bon = round(cw_data.get('bonus', 0))
    
    cw_budget_ngr = round(cw_data.get('budget_ngr', 0))
    cw_budget_bonus = round(cw_data.get('budget_bonus', 0))
    
    cw_yoy_ngr = round(cw_data.get('ngr_yoy', 0))
    cw_yoy_nadp = round(cw_data.get('nadps_yoy', 0))
    cw_yoy_bon = round(cw_data.get('bonus_yoy', 0))
    
    p12_ngr_avg = round(p12_data.get('ngr', 0) / 12)
    p12_nadp_avg = round(p12_data.get('nadps', 0) / 12)
    p12_bon_avg = round(p12_data.get('bonus', 0) / 12)

    var_ngr_12 = calc_var(cw_ngr, p12_ngr_avg)
    var_ngr_yoy = calc_var(cw_ngr, cw_yoy_ngr)
    var_ngr_budget = calc_var(cw_ngr, cw_budget_ngr)
    
    var_act_12 = calc_var(true_cw_act, true_p12_act)
    var_act_yoy = calc_var(true_cw_act, true_yoy_act)
    
    var_nadp_12 = calc_var(cw_nadp, p12_nadp_avg)
    var_nadp_yoy = calc_var(cw_nadp, cw_yoy_nadp)
    
    var_bon_12 = calc_var(cw_bon, p12_bon_avg)
    var_bon_yoy = calc_var(cw_bon, cw_yoy_bon)
    var_bon_budget = calc_var(cw_bon, cw_budget_bonus)
    
    bonus_tile = ""
    if not hide_bonus:
        bonus_tile = f"""
        <div class="kpi-tile">
            <span class="kpi-title">Bonus Cost</span>
            <span class="kpi-value">&euro;{cw_bon:,.0f}</span>
            <div class="var-row"><span class="var-label">vs Budget</span> {format_var(var_bon_budget, is_cost=True, delta=cw_bon - cw_budget_bonus, is_currency=True)}</div>
            <div class="var-row"><span class="var-label">vs 12W Avg</span> {format_var(var_bon_12, is_cost=True, delta=cw_bon - p12_bon_avg, is_currency=True)}</div>
            <div class="var-row"><span class="var-label">YoY</span> {format_var(var_bon_yoy, is_cost=True, delta=cw_bon - cw_yoy_bon, is_currency=True)}</div>
        </div>
        """

    kpi_html = f"""
    <div class="kpi-grid">
        <div class="kpi-tile">
            <span class="kpi-title">NGR</span>
            <span class="kpi-value">&euro;{cw_ngr:,.0f}</span>
            <div class="var-row"><span class="var-label">vs Budget</span> {format_var(var_ngr_budget, delta=cw_ngr - cw_budget_ngr, is_currency=True)}</div>
            <div class="var-row"><span class="var-label">vs 12W Avg</span> {format_var(var_ngr_12, delta=cw_ngr - p12_ngr_avg, is_currency=True)}</div>
            <div class="var-row"><span class="var-label">YoY</span> {format_var(var_ngr_yoy, delta=cw_ngr - cw_yoy_ngr, is_currency=True)}</div>
        </div>
        <div class="kpi-tile">
            <span class="kpi-title">Actives</span>
            <span class="kpi-value">{true_cw_act:,.0f}</span>
            <div class="var-row"><span class="var-label">vs 12W Avg</span> {format_var(var_act_12, delta=true_cw_act - true_p12_act)}</div>
            <div class="var-row"><span class="var-label">YoY</span> {format_var(var_act_yoy, delta=true_cw_act - true_yoy_act)}</div>
        </div>
        <div class="kpi-tile">
            <span class="kpi-title">NADPs</span>
            <span class="kpi-value">{cw_nadp:,.0f}</span>
            <div class="var-row"><span class="var-label">vs 12W Avg</span> {format_var(var_nadp_12, delta=cw_nadp - p12_nadp_avg)}</div>
            <div class="var-row"><span class="var-label">YoY</span> {format_var(var_nadp_yoy, delta=cw_nadp - cw_yoy_nadp)}</div>
        </div>
        {bonus_tile}
    </div>
    """

    product_items = ""
    for prod_key, prod_display in [('digital_casino', 'Digital Casino'), ('live_casino', 'Live Casino'), ('sb', 'Sportsbook'), ('poker', 'Poker')]:
        cw_p_ngr = round(cw_df[cw_df['product'] == prod_key]['ngr'].sum())
        if cw_p_ngr == 0: continue 
        
        cw_p_budget = round(cw_df[cw_df['product'] == prod_key]['budget_ngr'].sum())
        p12_p_ngr = round(p12_df[p12_df['product'] == prod_key]['ngr'].sum() / 12)
        cw_yoy_p_ngr = round(cw_df[cw_df['product'] == prod_key]['ngr_yoy'].sum())
        
        var_p_budget = calc_var(cw_p_ngr, cw_p_budget)
        var_p_12 = calc_var(cw_p_ngr, p12_p_ngr)
        var_p_yoy = calc_var(cw_p_ngr, cw_yoy_p_ngr)
        
        product_items += f"""
        <div class="product-item">
            <span class="product-name">{prod_display}:</span> 
            <span>&euro;{cw_p_ngr:,.0f} &bull; {format_var(var_p_budget, delta=cw_p_ngr - cw_p_budget, is_currency=True)} vs budget &bull; {format_var(var_p_12, delta=cw_p_ngr - p12_p_ngr, is_currency=True)} vs 12W avg &bull; {format_var(var_p_yoy, delta=cw_p_ngr - cw_yoy_p_ngr, is_currency=True)} YoY</span>
        </div>
        """
        
    prod_html = f'<div class="product-breakdown">{product_items}</div>' if product_items else ""
    return kpi_html + prod_html

country_html, brand_html = "", ""

for country in valid_countries:
    hide_bonuses_for_region = (country == 'Lithuania')
    brands_in_country = df_brands[df_brands['Country'] == country]['domain'].unique()
    brands = [b for b in BRAND_ORDER if b in brands_in_country]
    
    cw_df_c = df_b_cw[df_b_cw['Country'] == country]
    p12_df_c = df_b_12w[df_b_12w['Country'] == country]
    
    true_cw_act_c = df_p_cw[(df_p_cw['Country'] == country) & cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0
    p12_country_df = df_p_12w[(df_p_12w['Country'] == country) & p12_active_mask]
    true_p12_act_c = p12_country_df.groupby('week_block')['external_id'].nunique().mean() if not p12_country_df.empty else 0
    true_yoy_act_c = df_p_yoy[(df_p_yoy['Country'] == country) & yoy_active_mask]['external_id'].nunique() if not df_p_yoy.empty else 0
    
    country_html += f"""
    <div class="summary-card" style="border-top-color: #0984e3;">
        <div class="card-header">{country}</div>
        {generate_summary_card_content(cw_df_c, p12_df_c, true_cw_act_c, true_p12_act_c, true_yoy_act_c, hide_bonus=hide_bonuses_for_region)}
    </div>
    """
    
    for brand in brands:
        cw_df_b = df_b_cw[df_b_cw['domain'] == brand]
        p12_df_b = df_b_12w[df_b_12w['domain'] == brand]
        brand_color_hex = get_brand_color(brand)
        
        true_cw_act_b = df_p_cw[(df_p_cw['domain'] == brand) & cw_active_mask]['external_id'].nunique() if not df_p_cw.empty else 0
        p12_brand_df = df_p_12w[(df_p_12w['domain'] == brand) & p12_active_mask]
        true_p12_act_b = p12_brand_df.groupby('week_block')['external_id'].nunique().mean() if not p12_brand_df.empty else 0
        true_yoy_act_b = df_p_yoy[(df_p_yoy['domain'] == brand) & yoy_active_mask]['external_id'].nunique() if not df_p_yoy.empty else 0
        
        b_html_block = f"""
        <div class="summary-card" style="border-top-color: {brand_color_hex};">
            <div class="card-header">{brand}</div>
            {generate_summary_card_content(cw_df_b, p12_df_b, true_cw_act_b, true_p12_act_b, true_yoy_act_b, hide_bonus=hide_bonuses_for_region)}
        """
        
        if not outliers_df.empty and brand in outliers_df['domain'].values:
            b_outs = outliers_df[outliers_df['domain'] == brand]
            pos_outs = b_outs[b_outs['impact_type'] == 'Positive (Player Lost)']
            neg_outs = b_outs[b_outs['impact_type'] == 'Negative (Player Won)']
            
            pos_top3 = pos_outs.head(3)
            neg_top3 = neg_outs.head(3)
            
            pos_str = "<br/>".join([f"ID {row['external_id']} (&euro;{row['ngr']:,.0f})" for _, row in pos_top3.iterrows()]) or "None"
            neg_str = "<br/>".join([f"ID {row['external_id']} (&euro;{row['ngr']:,.0f})" for _, row in neg_top3.iterrows()]) or "None"
            
            pos_impact_top3 = pos_top3['ngr'].sum()
            neg_impact_top3 = neg_top3['ngr'].sum()
            
            b_html_block += f"""
            <div class="outlier-box">
                <p>Extreme Outliers (3.0x IQR, >&euro;1k):</p>
                <div class="outlier-grid">
                    <div class="outlier-tile outlier-pos">
                        <span class="outlier-title">House Won (Positive)</span>
                        <span class="outlier-total">&euro;{pos_impact_top3:,.0f} <span class="outlier-sub">Top 3 Contrib.</span></span>
                        <div class="outlier-details">{pos_str}</div>
                    </div>
                    <div class="outlier-tile outlier-neg">
                        <span class="outlier-title">House Lost (Negative)</span>
                        <span class="outlier-total">&euro;{neg_impact_top3:,.0f} <span class="outlier-sub">Top 3 Impact</span></span>
                        <div class="outlier-details">{neg_str}</div>
                    </div>
                </div>
            </div>
            """
        b_html_block += "</div>"
        brand_html += b_html_block

# ==========================================
# 6. ASSEMBLE HTML DASHBOARD
# ==========================================
print("Generating HTML Dashboard...")

html_output = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Weekly Performance Report</title>
<style>
    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #2d3436; font-size: 13px; background-color: #f4f6f8; margin: 0; padding: 20px; }}
    .container {{ max-width: 1600px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-radius: 8px; overflow-x: auto; }}
    
    .cover-title {{ font-size: 28pt; color: #0984e3; font-weight: bold; margin-bottom: 5px; }}
    .cover-subtitle {{ font-size: 16pt; color: #636e72; margin-bottom: 25px; border-bottom: 2px solid #dfe6e9; padding-bottom: 20px; }}
    
    .toc {{ background-color: #f9f9f9; border-left: 5px solid #0984e3; padding: 15px 25px; margin-bottom: 25px; width: max-content; }}
    .toc h2 {{ font-size: 14pt; margin: 0 0 10px 0; color: #2d3436; }}
    .toc-item {{ font-size: 11pt; font-weight: bold; margin-bottom: 5px; }}
    .toc-sub {{ font-size: 10pt; color: #636e72; margin-left: 20px; margin-bottom: 5px; }}
    .toc a {{ text-decoration: none; color: #2d3436; }}
    .toc a:hover {{ text-decoration: underline; color: #0984e3; }}
    
    .section-title {{ color: #0984e3; font-size: 18pt; border-bottom: 2px solid #dfe6e9; padding-bottom: 8px; margin-top: 40px; margin-bottom: 20px; }}
    .sub-table-title {{ font-size: 14pt; color: #2d3436; margin-top: 5px; margin-bottom: 10px; text-transform: uppercase; }}
    
    table.matrix {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; table-layout: auto; }}
    table.matrix th, table.matrix td {{ border: 1px solid #dfe6e9; padding: 8px 10px; text-align: right; white-space: nowrap; }}
    table.matrix th {{ background-color: #2d3436; color: white; text-align: center; font-weight: 600; }}
    
    .text-left {{ text-align: left !important; white-space: normal !important; }} 
    .text-center {{ text-align: center !important; }}
    
    .budget-cell {{ display: flex; align-items: center; justify-content: flex-end; padding: 0; }}
    .bar-wrapper {{ display: flex; width: 60px; height: 12px; align-items: center; margin-right: 12px; }}
    .bar-left {{ width: 50%; height: 100%; display: flex; justify-content: flex-end; padding-right: 1px; border-right: none; }}
    .bar-right {{ width: 50%; height: 100%; display: flex; justify-content: flex-start; padding-left: 1px; }}
    .fill-red {{ background-color: #d63031; height: 100%; border-radius: 2px 0 0 2px; }}
    .fill-green {{ background-color: #00b894; height: 100%; border-radius: 0 2px 2px 0; }}
    
    .total-row {{ background-color: #f1f2f6; font-weight: bold; color: #2d3436; }}
    .overall-row {{ background-color: #2d3436; color: #ffffff; font-weight: bold; }}
    .overall-row td {{ border-color: #636e72; }}
    
    .positive {{ color: #00b894; font-weight: bold; }}
    .negative {{ color: #d63031; font-weight: bold; }}
    
    .summary-card {{ background: white; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); padding: 25px; margin-bottom: 25px; border-top: 5px solid; }}
    .card-header {{ font-size: 20px; font-weight: bold; color: #2d3436; margin-bottom: 20px; text-transform: uppercase; }}
    
    .kpi-grid {{ display: flex; gap: 15px; margin-bottom: 20px; }}
    .kpi-tile {{ flex: 1; background: #f8f9fa; border: 1px solid #dfe6e9; border-radius: 8px; padding: 15px; display: flex; flex-direction: column; align-items: center; }}
    .kpi-title {{ font-size: 11px; color: #636e72; text-transform: uppercase; font-weight: bold; letter-spacing: 0.5px; margin-bottom: 5px; }}
    .kpi-value {{ font-size: 24px; font-weight: 800; color: #2d3436; margin-bottom: 15px; }}
    
    .var-row {{ display: flex; justify-content: space-between; width: 100%; font-size: 12px; border-bottom: 1px solid #eee; padding-bottom: 4px; margin-bottom: 4px; }}
    .var-row:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
    .var-label {{ color: #636e72; }}
    
    .product-breakdown {{ font-size: 13px; color: #2d3436; margin-bottom: 20px; padding: 15px; background-color: #fcfcfc; border-radius: 8px; border-left: 3px solid #0984e3; }}
    .product-item {{ display: flex; margin-bottom: 5px; }}
    .product-item:last-child {{ margin-bottom: 0; }}
    .product-name {{ width: 110px; font-weight: 600; }}
    
    /* Outlier Scorecard Styling */
    .outlier-box {{ background-color: #f8f9fa; border-left: 4px solid #f1c40f; padding: 15px; font-size: 13px; border-radius: 4px; }}
    .outlier-box p {{ margin: 0 0 10px 0; font-weight: bold; color: #b38800; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }}
    
    .outlier-grid {{ display: flex; gap: 15px; }}
    .outlier-tile {{ flex: 1; border-radius: 6px; padding: 12px; display: flex; flex-direction: column; background: #ffffff; }}
    .outlier-pos {{ border-top: 4px solid #00b894; box-shadow: 0 2px 4px rgba(0,184,148,0.1); }}
    .outlier-neg {{ border-top: 4px solid #d63031; box-shadow: 0 2px 4px rgba(214,48,49,0.1); }}
    
    .outlier-title {{ font-size: 11px; color: #636e72; text-transform: uppercase; font-weight: bold; margin-bottom: 5px; }}
    .outlier-total {{ font-size: 18px; font-weight: 800; color: #2d3436; margin-bottom: 10px; display: flex; align-items: baseline; gap: 8px; }}
    .outlier-sub {{ font-size: 10px; font-weight: normal; color: #b2bec3; text-transform: none; }}
    .outlier-details {{ font-size: 12px; color: #636e72; line-height: 1.5; }}
</style>
</head>
<body>
<div class="container">
    <div class="cover-title">Weekly Performance Report</div>
    <div class="cover-subtitle">Week of {title_date_str}</div>

    <div class="toc">
        <h2>Table of Contents</h2>
        <div class="toc-item"><a href="#performance-snapshot">1. Performance Snapshot</a></div>
        
        <div class="toc-item"><a href="#product-performance">2. Product Performance</a></div>
        <div class="toc-sub">- Deep Dive by Country</div>
        
        <div class="toc-item"><a href="#kpi-performance">3. KPI Performance</a></div>
        <div class="toc-sub">- Deep Dive by Country</div>
        
        <div class="toc-item"><a href="#country-summary">4. Country Summary</a></div>
        <div class="toc-sub">- KPI Performance vs Budget, 12-Week Avg & YoY</div>
        
        <div class="toc-item"><a href="#brand-summary">5. Brand Summary & Outliers</a></div>
        <div class="toc-sub">- Brand Level Variances (Extreme Player Outliers)</div>
    </div>

    <a name="performance-snapshot"></a>
    <h1 class="section-title">1. Performance Snapshot</h1>
    <table class="matrix">
        <thead>
            <tr>
                <th class="text-center">Country</th>
                <th class="text-left" style="width: 12%;">Brand</th>
                <th>Actives</th><th>NADP</th><th>Conv.</th><th>Turnover</th>
                <th>GGR</th><th>GGR %</th><th>Dig. Casino</th><th>Live Casino</th>
                <th>Sports</th><th>Poker</th><th>Bonus</th><th>Bonus %</th>
                <th>NGR</th><th>vs Budget</th><th>Budget</th>
            </tr>
        </thead>
        <tbody>
"""

current_country = ""
for row in final_matrix_rows:
    is_total = row.get('is_total', False)
    is_overall = row.get('is_overall', False)
    country_name = row['Country']
    
    row_class = 'overall-row' if is_overall else ('total-row' if is_total else '')
    vs_budget_class = 'positive' if row['NGR vs Budget'] >= 0 else 'negative'
    
    html_output += f'<tr class="{row_class}">'
    if is_overall:
        html_output += f'<td class="text-center" colspan="2" style="font-size: 11pt;">OVERALL TOTAL</td>'
    else:
        if country_name != current_country:
            rowspan = len([r for r in final_matrix_rows if r['Country'] == country_name and not r.get('is_overall')])
            html_output += f'<td rowspan="{rowspan}" style="font-weight: bold; color: #0984e3; text-align: center; vertical-align: middle;">{country_name}</td>'
            current_country = country_name
        html_output += f'<td class="text-left">{row["domain"]}</td>'
        
    html_output += f"""
            <td>{row.get('actives', 0):,.0f}</td><td>{row.get('nadps', 0):,.0f}</td><td>{row.get('Conversion Rate', 0):.1%}</td>
            <td>&euro;{row.get('turnover', 0):,.0f}</td><td>&euro;{row.get('ggr', 0):,.0f}</td><td>{row.get('GGR %', 0):.1%}</td>
            <td>&euro;{row.get('digital_casino', 0):,.0f}</td><td>&euro;{row.get('live_casino', 0):,.0f}</td>
            <td>&euro;{row.get('sb', 0):,.0f}</td><td>&euro;{row.get('poker', 0):,.0f}</td>
            <td>&euro;{row.get('bonus', 0):,.0f}</td><td>{row.get('Bonus %', 0):.1%}</td><td>&euro;{row.get('ngr', 0):,.0f}</td>
            <td class="{vs_budget_class}">{row.get('NGR vs Budget', 0):.1%}</td><td>&euro;{row.get('budget_ngr', 0):,.0f}</td>
        </tr>
    """

html_output += f"""
        </tbody>
    </table>

    <a name="product-performance"></a>
    <h1 class="section-title">2. Product Performance</h1>
    {product_tables_html}

    <a name="kpi-performance"></a>
    <h1 class="section-title">3. KPI Performance</h1>
    {kpi_tables_html}

    <a name="country-summary"></a>
    <h1 class="section-title">4. Country Summary</h1>
    {country_html}

    <a name="brand-summary"></a>
    <h1 class="section-title">5. Brand Summary & Outliers</h1>
    {brand_html}
</div>
</body>
</html>
"""

# Create the outputs folder if it doesn't exist
output_dir = "outputs/weekly"
os.makedirs(output_dir, exist_ok=True)

# Define the file name and the full path
output_filename = f"Weekly_Performance_Report_{datetime.now().strftime('%Y-%m-%d')}.html"
output_filepath = os.path.join(output_dir, output_filename)

# Save the file to the new folder
with open(output_filepath, "w", encoding="utf-8") as result_file:
    result_file.write(html_output)

print(f"✅ Success! Dashboard generated: {output_filepath}")