#!/usr/bin/env python3
"""V3 free A-share daily scanner. Public market data only; never publish holdings.
Requires baostock. No buy/sell orders. Offline self-test: python screener.py --self-test
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
DATA = Path('data')
FIELDS = ('date,code,close,pctChg,amount,turn,peTTM,pbMRQ,isST,tradestatus')
MIN_UNIVERSE = 3500
MIN_COVERAGE = 0.86
# BaoStock does not provide total / float market cap in this API: intentionally omitted.
RULES = {
    'min_amount_yuan': 100_000_000,
    'min_turn_pct': 1.0, 'max_turn_pct': 15.0,
    'min_pe_ttm': 0.0, 'max_pe_ttm': 40.0,
    'min_pb_mrq': 0.5, 'max_pb_mrq': 6.0,
    'min_60d_pct': -15.0, 'max_60d_pct': 40.0,
    'min_price_yuan': 3.0, 'max_day_pct': 7.0,
    'top_n': 30,
}


def num(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        n = float(str(value).replace(',', ''))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def allowed_equity(code):
    """Exclude known ETFs, indices, B shares and bonds (conservative prefixes)."""
    if re.fullmatch(r'sh\.(600|601|603|605|688|689)\d{3}', code):
        return True
    if re.fullmatch(r'sz\.(000|001|002|003|300|301)\d{3}', code):
        return True
    # Beijing Stock Exchange where BaoStock provides a matching code.
    if re.fullmatch(r'bj\.(?:4\d{5}|8\d{5}|92\d{4})', code):
        return True
    return False


def clean_code(code):
    return code.replace('.', '').lower()


def read_bs_rows(rs):
    if rs.error_code != '0':
        raise RuntimeError(f'BaoStock error: {rs.error_code} {rs.error_msg}')
    rows = []
    while rs.next():
        rows.append(dict(zip(rs.fields, rs.get_row_data())))
    return rows


def latest_day(bar_rows, day):
    """Select exactly today's bar; fail closed on lag or suspended stock."""
    rows = [r for r in bar_rows if r.get('date') and num(r.get('close')) and num(r.get('close')) > 0]
    if not rows or rows[-1]['date'] != day or rows[-1].get('tradestatus') != '1':
        return None
    r = rows[-1]
    last_60 = None
    if len(rows) >= 61:
        prev = num(rows[-61].get('close'))
        now = num(r.get('close'))
        if prev and now:
            last_60 = 100 * (now / prev - 1)
    return {
        'date': day, 'code': clean_code(r['code']),
        'close': num(r.get('close')), 'pct_change': num(r.get('pctChg')),
        'amount_yuan': num(r.get('amount')), 'turn_pct': num(r.get('turn')),
        'pe_ttm': num(r.get('peTTM')), 'pb_mrq': num(r.get('pbMRQ')),
        'return_60d_pct': last_60, 'is_st': str(r.get('isST')) == '1',
        'history_bars': len(rows),
    }


def eligible(r):
    a = RULES
    if r.get('is_st') or re.search(r'\*?ST|退市|^退', r.get('name', ''), flags=re.I):
        return False
    needed = ('close', 'pct_change', 'amount_yuan', 'turn_pct', 'pe_ttm', 'pb_mrq', 'return_60d_pct')
    if any(r.get(k) is None for k in needed):
        return False
    return (r['close'] >= a['min_price_yuan'] and
            r['amount_yuan'] >= a['min_amount_yuan'] and
            a['min_turn_pct'] <= r['turn_pct'] <= a['max_turn_pct'] and
            a['min_pe_ttm'] < r['pe_ttm'] <= a['max_pe_ttm'] and
            a['min_pb_mrq'] <= r['pb_mrq'] <= a['max_pb_mrq'] and
            a['min_60d_pct'] <= r['return_60d_pct'] <= a['max_60d_pct'] and
            -9.8 <= r['pct_change'] <= a['max_day_pct'])


def grade(r):
    # An explicit, interpretable heuristic, NOT an expected return / buy signal.
    turnover_billion = r['amount_yuan'] / 1e8
    s = min(20, 8 + 8 * math.log10(max(1, turnover_billion)))
    s += max(0, 20 - abs(r['return_60d_pct'] - 15) * 0.65)
    s += max(0, 15 - abs(r['turn_pct'] - 5) * 1.7)
    s += max(0, 20 - abs(r['pe_ttm'] - 18) * 0.7)
    s += max(0, 15 - abs(r['pb_mrq'] - 2) * 2.5)
    s += max(0, 10 - abs(r['pct_change'] - 0.5) * 2)
    return round(min(100, max(0, s)), 1)


def formatted(v, digits=3):
    return '' if v is None else f'{v:.{digits}f}'


def export(data, today, universe_total, failures):
    DATA.mkdir(parents=True, exist_ok=True)
    captured = datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')
    markets = Counter(x['code'][:2] for x in data)
    market_fields = ['date', 'code', 'name', 'close', 'pct_change', 'amount_yuan',
                     'turn_pct', 'pe_ttm', 'pb_mrq', 'return_60d_pct', 'is_st',
                     'history_bars', 'captured_at_bj', 'source']
    # All market metrics in GitHub are PUBLIC; private holdings never enter this script.
    with (DATA / 'market_latest.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=market_fields)
        writer.writeheader()
        for r in data:
            writer.writerow({**r, 'captured_at_bj': captured, 'source': 'BaoStock daily',
                             'is_st': int(r['is_st'])})
    passing = sorted((r for r in data if eligible(r)),
                     key=lambda r: (-grade(r), -(r['amount_yuan'] or 0), r['code']))
    top = passing[:RULES['top_n']]
    fields = ['rank', 'date', 'code', 'name', 'score', 'close', 'pct_change',
              'amount_100m', 'turn_pct', 'pe_ttm', 'pb_mrq', 'return_60d_pct',
              'source', 'research_note']
    with (DATA / 'candidates_latest.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for idx, r in enumerate(top, 1):
            writer.writerow({
                'rank': idx, 'date': today, 'code': r['code'], 'name': r['name'],
                'score': grade(r), 'close': formatted(r['close']),
                'pct_change': formatted(r['pct_change'], 2),
                'amount_100m': formatted(r['amount_yuan'] / 1e8, 2),
                'turn_pct': formatted(r['turn_pct'], 2),
                'pe_ttm': formatted(r['pe_ttm'], 2),
                'pb_mrq': formatted(r['pb_mrq'], 2),
                'return_60d_pct': formatted(r['return_60d_pct'], 2),
                'source': 'BaoStock daily',
                'research_note': '仅日级初筛；另查财报/风险公告/行业；非交易建议'
            })
    manifest = {
        'schema_version': '3.0', 'data_date': today,
        'generated_at_bj': captured, 'source': 'BaoStock daily',
        'universe_count': universe_total, 'valid_count': len(data),
        'coverage_rate': round(len(data) / universe_total, 4),
        'failed_or_suspended_count': failures,
        'exchange_counts': markets, 'screen_pass_count': len(passing),
        'top_count': len(top), 'filters': RULES,
        'known_limitations': [
            'No total/float market capitalization: market-cap rules intentionally not applied',
            'BaoStock stock-universe coverage (especially BJ) must be inspected; no full-BJ guarantee',
            '60-day return uses a 61-bar adjusted close series; no daily fundamentals audit',
            'Free third-party source can fail or lag; never treat ranking as trading signal',
            'Public GitHub repository: no positions, cash, account details or personal identifiers',
        ],
    }
    (DATA / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in manifest.items() if k != 'filters'}, ensure_ascii=False, indent=2))


def run():
    import baostock as bs
    now = datetime.now(BJ)
    today = now.strftime('%Y-%m-%d')
    print('China date:', today, 'time:', now.isoformat(), flush=True)
    if now.hour < 17:
        print('Before 17:00 Beijing; skip and retain last result.')
        return 0
    login = bs.login()
    if login.error_code != '0':
        raise RuntimeError('BaoStock login failed: ' + str(login.error_msg))
    try:
        td = read_bs_rows(bs.query_trade_dates(start_date=today, end_date=today))
        if not td or td[-1].get('is_trading_day') != '1':
            print('Non-trading day, no new publication.')
            return 0
        items = read_bs_rows(bs.query_all_stock(today))
        universe = [(x['code'], x.get('code_name', x['code'])) for x in items
                    if allowed_equity(x.get('code', ''))]
        if len(universe) < MIN_UNIVERSE:
            raise RuntimeError(f'Universe too small: {len(universe)}, refuses to overwrite')
        print('Universe', len(universe), flush=True)
        start = (now.date() - timedelta(days=145)).isoformat()
        data, failed = [], 0
        for i, (code, name) in enumerate(universe, 1):
            try:
                hist = read_bs_rows(bs.query_history_k_data_plus(
                    code, FIELDS, start_date=start, end_date=today,
                    frequency='d', adjustflag='2'))
                latest = latest_day(hist, today)
                if latest is None:
                    failed += 1
                else:
                    latest['name'] = name
                    data.append(latest)
            except Exception as exc:
                failed += 1
                if failed <= 10 or failed % 100 == 0:
                    print('Warning:', code, str(exc)[:180], flush=True)
            if i % 500 == 0:
                print(f'Progress {i}/{len(universe)}, valid {len(data)}, failed {failed}', flush=True)
            time.sleep(0.01)  # Respect free service; increase if rate-limited.
        rate = len(data) / len(universe)
        if len(data) < MIN_UNIVERSE or rate < MIN_COVERAGE:
            raise RuntimeError(f'Incomplete market: {len(data)}/{len(universe)} ({rate:.1%}); old release retained')
        export(data, today, len(universe), failed)
    finally:
        bs.logout()
    return 0


def self_test():
    assert allowed_equity('sh.600000') and allowed_equity('sz.300750')
    assert allowed_equity('bj.920001') and not allowed_equity('sh.000001')
    assert not allowed_equity('sz.399001') and not allowed_equity('sh.900901')
    rows = [{'date': '2026-10-22', 'code': 'sh.600000', 'close': '8.00',
             'pctChg': '1.2', 'amount': '220000000', 'turn': '3.1',
             'peTTM': '12', 'pbMRQ': '1.4', 'isST': '0', 'tradestatus': '1'}]
    r = latest_day(rows, '2026-10-22')
    assert r and r['return_60d_pct'] is None
    rows = [dict(rows[0], date=f'2026-07-{(i % 28) + 1:02d}', close='7.00')
            for i in range(60)] + rows
    r = latest_day(rows, '2026-10-22')
    r['name'] = '测试股票'
    assert round(r['return_60d_pct'], 2) == 14.29
    assert eligible(r) and 0 <= grade(r) <= 100
    assert not eligible({**r, 'is_st': True})
    print('SELF TEST PASS (not a live network test)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        try:
            sys.exit(run())
        except Exception as e:
            print('FATAL:', str(e), file=sys.stderr, flush=True)
            sys.exit(1)
