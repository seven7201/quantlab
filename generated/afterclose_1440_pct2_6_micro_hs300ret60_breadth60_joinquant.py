# -*- coding: utf-8 -*-
"""
JoinQuant 策略：afterclose_1440_pct2_6_micro_hs300ret60_breadth60

由 ptrade-quant-lab 按 PTrade 主策略迁移生成，供 joinquant.com 回测使用。

对应本地策略：
/Users/mac/app/ptrade-quant-lab/strategies/momentum_macd_top5/tmp_40k_variants/40k_afterclose_1440_pct2_6_micro.yaml

策略保持不变：
- 市场开仓过滤：沪深300代理 ret60 > 5%，全A MA20市场宽度 > 60%，市场未缩量。
- 选股：涨幅 2%-6%、换手 3%-20%、量比 > 1.3、站上 MA20 且 MA20 上行、近3日 MACD 金叉、20日均振幅 >= 4%、近20日 MA10/MA20 同时上行。
- 排名：按当日涨幅降序，最多保留2只候选。
- 风控：单票33%，最多6只，每日最多买1只；跌破MA20/止损8%/浮盈20%后回撤8%且跌破MA10 卖出。

JoinQuant API 适配说明：
- 历史行情使用 get_price(..., count=N, frequency='daily', fields=[...], fq='pre', panel=False)。
- 股票池使用 get_all_securities(['stock'], date=context.current_dt)。
- 下单使用 order_value / order_target_value。
- 换手率优先使用 get_valuation 的 turnover_ratio；失败时该股票不入选，以保持策略条件不被放松。

把本文件完整复制到 JoinQuant 策略编辑器即可。
"""

import math
import datetime

import pandas as pd


PARAMS = {
    'stock_pool': [],  # 留空则扫描全A；也可填 ['000001.XSHE', '600000.XSHG']
    'max_position_pct': 0.33,
    'max_daily_buys': 1,
    'max_positions': 6,
    'stop_loss_pct': 0.08,
    'trailing_activation_pct': 0.20,
    'trailing_stop_pct': 0.08,
    'min_turnover_pct': 3.0,
    'max_turnover_pct': 20.0,
    'min_volume_ratio': 1.3,
    'min_pct_chg': 2.0,
    'max_pct_chg': 6.0,
    'macd_cross_within_days': 3,
    'min_avg_amplitude_20d_pct': 4.0,
    'save_top_n': 2,
    'hs300_proxy_ret60_min': 0.05,
    'ma20_breadth_min': 0.60,
    'amount_ma20_ratio_min': 0.80,
}

STRATEGY_VERSION = 'jq_amp_shared_calc_20260602_v5'


def initialize(context):
    set_params(context)
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    log.set_level('order', 'info')

    # 每天盘前生成候选，开盘后执行卖出/买入，收盘记录。
    run_daily(before_market_open, time='before_open')
    run_daily(trade, time='09:35')
    run_daily(after_market_close, time='after_close')

    log.info('afterclose_1440_pct2_6_micro_hs300ret60_breadth60 initialized version=%s' % STRATEGY_VERSION)


def set_params(context):
    g.params = PARAMS
    g.today_pool = []
    g.hold_cost = {}
    g.highest_price = {}


def before_market_open(context):
    if not is_market_regime_ok(context):
        g.today_pool = []
        log.info('market regime blocked new positions')
        return
    g.today_pool = select_today_pool(context)
    log.info('today candidates: %s' % g.today_pool)


def trade(context):
    sell_positions(context)
    buy_candidates(context)


def after_market_close(context):
    log.info('day end, candidates=%s' % g.today_pool)


def sell_positions(context):
    current_data = get_current_data()
    for stock in list(context.portfolio.positions.keys()):
        position = context.portfolio.positions[stock]
        if position.total_amount <= 0:
            continue
        if current_data[stock].paused:
            continue

        hist = get_daily_price(stock, 35, ['close'], context)
        if hist is None or len(hist) < 21:
            continue

        close = hist['close']
        current_price = safe_float(close.iloc[-1])
        ma20 = safe_float(close.iloc[-20:].mean())
        ma10 = safe_float(close.iloc[-10:].mean())
        cost = g.hold_cost.get(stock, safe_float(position.avg_cost) or current_price)
        pnl = current_price / cost - 1 if cost else 0.0

        g.highest_price[stock] = max(safe_float(g.highest_price.get(stock, current_price)), current_price)
        high = safe_float(g.highest_price[stock])
        drawdown_from_high = current_price / high - 1 if high else 0.0

        reason = None
        if pnl <= -g.params['stop_loss_pct']:
            reason = 'stop_loss_8pct'
        elif current_price < ma20:
            reason = 'close_below_ma20'
        elif pnl >= g.params['trailing_activation_pct'] and drawdown_from_high <= -g.params['trailing_stop_pct'] and current_price < ma10:
            reason = 'trailing_20_8_below_ma10'

        if reason:
            order_target_value(stock, 0)
            g.hold_cost.pop(stock, None)
            g.highest_price.pop(stock, None)
            log.info('%s sell: %s' % (stock, reason))


def buy_candidates(context):
    current_data = get_current_data()
    open_positions = [s for s, p in context.portfolio.positions.items() if p.total_amount > 0]
    slots = max(g.params['max_positions'] - len(open_positions), 0)
    if slots <= 0:
        return

    bought = 0
    for stock in g.today_pool[:g.params['save_top_n']]:
        if bought >= g.params['max_daily_buys'] or bought >= slots:
            break
        if stock in context.portfolio.positions and context.portfolio.positions[stock].total_amount > 0:
            continue
        if stock not in current_data or current_data[stock].paused:
            continue
        if is_limit_up_now(stock, current_data):
            continue

        target_value = context.portfolio.total_value * g.params['max_position_pct']
        order_value(stock, target_value)
        price = safe_float(current_data[stock].last_price)
        if price:
            g.hold_cost[stock] = price
            g.highest_price[stock] = price
        bought += 1
        log.info('%s buy candidate, target_value=%.2f' % (stock, target_value))


def select_today_pool(context):
    current_data = get_current_data()
    trade_date = get_trade_date(context)
    universe = get_candidate_universe(context)
    turnover_map = get_turnover_ratio_map(universe, trade_date)

    candidates = []
    pre_amp_survivors = []
    pre_amp_debug_logged = 0
    stats = {
        'universe': len(universe),
        'skip': 0,
        'no_price': 0,
        'no_turnover': 0,
        'indicator_failed': 0,
        'pct_chg': 0,
        'turnover': 0,
        'volume_ratio': 0,
        'ma20': 0,
        'macd': 0,
        'amp20': 0,
        'ma10_ma20_up20': 0,
        'pass': 0,
    }
    for stock in universe:
        if should_skip_stock(stock, current_data):
            stats['skip'] += 1
            continue
        hist = get_daily_price(stock, 90, ['open', 'high', 'low', 'close', 'volume', 'money'], context)
        if hist is None or len(hist) < 61:
            stats['no_price'] += 1
            continue

        turnover = turnover_map.get(stock)
        if turnover is None:
            stats['no_turnover'] += 1
            continue

        try:
            close = hist['close']
            volume = hist['volume']
            current = safe_float(close.iloc[-1])
            prev = safe_float(close.iloc[-2])
            pct_chg = (current / prev - 1) * 100 if prev else 0.0
            ma20 = safe_float(close.iloc[-20:].mean())
            prev_ma20 = safe_float(close.iloc[-21:-1].mean())
            vol_ma5 = safe_float(volume.iloc[-6:-1].mean())
            volume_ratio = safe_float(volume.iloc[-1]) / vol_ma5 if vol_ma5 else 0.0
            avg_amp20 = calc_avg_amplitude_pct(hist, 20)
            dif, dea = calc_macd(close)
            cross_age = macd_cross_age(dif, dea, g.params['macd_cross_within_days'])
            ma10_ma20_both_up_recent20 = check_ma10_ma20_both_up_recent20(close)
        except Exception as e:
            stats['indicator_failed'] += 1
            log.info('%s indicator failed: %s' % (stock, e))
            continue

        if not (g.params['min_pct_chg'] <= pct_chg <= g.params['max_pct_chg']):
            stats['pct_chg'] += 1
            continue
        if not (g.params['min_turnover_pct'] <= turnover <= g.params['max_turnover_pct']):
            stats['turnover'] += 1
            continue
        if volume_ratio <= g.params['min_volume_ratio']:
            stats['volume_ratio'] += 1
            continue
        if current <= ma20 or ma20 <= prev_ma20:
            stats['ma20'] += 1
            continue
        if cross_age is None:
            stats['macd'] += 1
            continue
        pre_amp_survivors.append((stock, pct_chg, turnover, volume_ratio, avg_amp20, cross_age))
        if avg_amp20 < g.params['min_avg_amplitude_20d_pct']:
            if pre_amp_debug_logged < 5:
                log.info('%s pre_amp_debug version=%s pct=%.2f turnover=%.2f vr=%.2f amp=%.4f age=%s tail=%s calc_diag=%s' % (stock, STRATEGY_VERSION, pct_chg, turnover, volume_ratio, avg_amp20, cross_age, get_amp_debug_tail(hist), get_amp_calc_diag(hist, 20)))
                pre_amp_debug_logged += 1
            stats['amp20'] += 1
            continue
        if not ma10_ma20_both_up_recent20:
            stats['ma10_ma20_up20'] += 1
            continue

        stats['pass'] += 1
        candidates.append((stock, pct_chg))

    candidates.sort(key=lambda x: x[1], reverse=True)
    log.info('select stats: %s' % stats)
    if pre_amp_survivors:
        amp_top = sorted(pre_amp_survivors, key=lambda x: x[4], reverse=True)[:10]
        log.info(
            'select pre-amp top_by_amp: %s' % [
                (s, round(p, 2), round(t, 2), round(vr, 2), round(amp, 2), age)
                for s, p, t, vr, amp, age in amp_top
            ]
        )
    if candidates:
        log.info('select raw top: %s' % candidates[:10])
    return [s for s, _ in candidates[:g.params['save_top_n']]]


def is_market_regime_ok(context):
    current_data = get_current_data()
    universe = get_candidate_universe(context)
    rows = []
    skip_count = 0
    no_price_count = 0

    for stock in universe:
        if should_skip_stock(stock, current_data):
            skip_count += 1
            continue
        hist = get_daily_price(stock, 90, ['close', 'money'], context)
        if hist is None or len(hist) < 61:
            no_price_count += 1
            continue
        try:
            close = hist['close']
            money = hist['money']
            current = safe_float(close.iloc[-1])
            close_60 = safe_float(close.iloc[-61])
            ma20 = safe_float(close.iloc[-20:].mean())
            ret60 = current / close_60 - 1 if close_60 else 0.0
            amt = safe_float(money.iloc[-1])
            amt_ma20 = safe_float(money.iloc[-20:].mean())
            rows.append({
                'stock': stock,
                'close': current,
                'ma20': ma20,
                'ret60': ret60,
                'money': amt,
                'money_ma20': amt_ma20,
            })
        except Exception:
            continue

    if not rows:
        log.info(
            'market regime no valid rows, universe_size=%s skip_count=%s no_price_count=%s; check JoinQuant daily data/end_date/current_data availability' % (
                len(universe), skip_count, no_price_count
            )
        )
        return False

    valid_breadth = [r for r in rows if r['ma20'] > 0]
    breadth = float(len([r for r in valid_breadth if r['close'] > r['ma20']])) / len(valid_breadth) if valid_breadth else 0.0

    # 沪深300代理：成交额前300只股票 ret60 等权平均，保持与本地 PTrade 版本的可执行代理口径一致。
    top300 = sorted(rows, key=lambda r: r['money'], reverse=True)[:300]
    proxy_ret60 = sum(r['ret60'] for r in top300) / len(top300) if top300 else 0.0

    total_money = sum(r['money'] for r in rows)
    total_money_ma20 = sum(r['money_ma20'] for r in rows)
    amount_ok = True
    if total_money_ma20 > 0:
        amount_ok = total_money >= total_money_ma20 * g.params['amount_ma20_ratio_min']

    proxy_ok = proxy_ret60 > g.params['hs300_proxy_ret60_min']
    breadth_ok = breadth > g.params['ma20_breadth_min']
    log.info(
        'market regime rows=%s proxy_ret60=%.2f%%>%s%%:%s breadth=%.2f%%>%s%%:%s amount_ok=%s' % (
            len(rows),
            proxy_ret60 * 100,
            g.params['hs300_proxy_ret60_min'] * 100,
            proxy_ok,
            breadth * 100,
            g.params['ma20_breadth_min'] * 100,
            breadth_ok,
            amount_ok,
        )
    )
    return proxy_ok and breadth_ok and amount_ok


def get_candidate_universe(context):
    if g.params.get('stock_pool'):
        return list(g.params['stock_pool'])
    securities = get_all_securities(['stock'], date=get_trade_date(context))
    stocks = list(securities.index)
    return [s for s in stocks if s.endswith('.XSHE') or s.endswith('.XSHG')]


def get_turnover_ratio_map(stocks, trade_date):
    result = {}
    if not stocks:
        return result
    # JoinQuant 单次 fundamentals 查询过长时可能报错，分批处理。
    batch_size = 500
    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i + batch_size]
        try:
            df = get_fundamentals(
                query(valuation.code, valuation.turnover_ratio).filter(valuation.code.in_(batch)),
                date=trade_date,
            )
        except Exception as e:
            log.info('get_turnover_ratio failed batch %s: %s' % (i, e))
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            result[str(row['code'])] = safe_float(row['turnover_ratio'])
    return result


def get_daily_price(stock, count, fields, context=None):
    end_date = get_price_end_date(context)
    try:
        df = get_price(
            stock,
            end_date=end_date,
            count=count,
            frequency='daily',
            fields=fields,
            fq='pre',
            panel=False,
        )
    except TypeError:
        # 兼容部分 JoinQuant 环境不支持 panel 参数。
        df = get_price(stock, end_date=end_date, count=count, frequency='daily', fields=fields, fq='pre')
    except Exception as e:
        log.info('%s get_price failed: %s' % (stock, e))
        return None

    if df is None or len(df) == 0:
        return None
    if isinstance(df, pd.Panel):
        # 老版本 panel=True 返回 Panel 时的兜底。
        df = df[:, :, stock]
    return df


def get_trade_date(context):
    # 盘前运行时 current_dt 可能还不是交易结束，用当前日期给 fundamentals 查询。
    try:
        return context.current_dt.date()
    except Exception:
        return None


def get_price_end_date(context):
    """JoinQuant 盘前/盘中日线取数应显式取上一交易日，避免拿不到当日未收盘日线。"""
    if context is None:
        return None
    try:
        current_date = context.current_dt.date()
        trade_days = get_trade_days(end_date=current_date, count=2)
        if len(trade_days) >= 2 and trade_days[-1] == current_date:
            return trade_days[-2]
        if len(trade_days) >= 1:
            return trade_days[-1]
    except Exception:
        pass
    try:
        return context.current_dt.date() - datetime.timedelta(days=1)
    except Exception:
        return None


def should_skip_stock(stock, current_data):
    if is_bj(stock):
        return True
    try:
        cd = current_data[stock]
    except Exception:
        # JoinQuant 的 current_data 在部分回测环境不支持 membership 判断；取不到才跳过。
        return True
    try:
        if getattr(cd, 'paused', False):
            return True
        name = getattr(cd, 'name', '') or ''
        if 'ST' in name.upper() or '*ST' in name.upper() or '退' in name:
            return True
    except Exception:
        return True
    return False


def is_bj(stock):
    # JoinQuant 北交所常见后缀 XBEI；兼容 8/4 开头代码。
    s = str(stock)
    return s.endswith('.XBEI') or s.startswith('8') or s.startswith('4')


def is_limit_up_now(stock, current_data):
    try:
        cd = current_data[stock]
        last_price = safe_float(cd.last_price)
        high_limit = safe_float(cd.high_limit)
        return high_limit > 0 and last_price >= high_limit
    except Exception:
        return False


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def macd_cross_age(dif, dea, max_days):
    for age in range(max_days):
        i = -1 - age
        j = -2 - age
        if abs(j) > len(dif):
            break
        if safe_float(dif.iloc[j]) <= safe_float(dea.iloc[j]) and safe_float(dif.iloc[i]) > safe_float(dea.iloc[i]):
            return age
    return None


def calc_avg_amplitude_pct(hist, n):
    diag = calc_amp_stats(hist, n, include_debug=False)
    return diag.get('avg', 0.0)


def calc_amp_stats(hist, n, include_debug=True):
    try:
        hist = normalize_price_df(hist)
        required = ['high', 'low', 'close']
        missing = [c for c in required if c not in hist.columns]
        if missing:
            return {'avg': 0.0, 'missing': missing, 'len': len(hist)}
        records = hist[required].to_dict('records')
        vals = []
        skips = []
        start = max(1, len(records) - n)
        for i in range(start, len(records)):
            row = records[i]
            prev = records[i - 1]
            try:
                high = float(row.get('high', 0) or 0)
                low = float(row.get('low', 0) or 0)
                prev_close = float(prev.get('close', 0) or 0)
                close = float(row.get('close', 0) or 0)
                base = prev_close if prev_close > 0 else close
                amp = (high - low) / base * 100.0 if base > 0 else 0.0
                if amp > 0 and amp < 100:
                    vals.append(amp)
                elif include_debug and len(skips) < 3:
                    skips.append({'i': i, 'h': high, 'l': low, 'pc': prev_close, 'c': close, 'base': base, 'amp': amp})
            except Exception as e:
                if include_debug and len(skips) < 3:
                    skips.append({'i': i, 'err': str(e), 'row': row, 'prev': prev})
        avg = sum(vals) / len(vals) if vals else 0.0
        result = {'avg': avg}
        if include_debug:
            result.update({
                'records_len': len(records),
                'tail2': records[-2:] if len(records) >= 2 else records,
                'vals_count': len(vals),
                'vals_head': [round(x, 4) for x in vals[:5]],
                'avg': round(avg, 4),
                'skips': skips,
            })
        return result
    except Exception as e:
        return {'avg': 0.0, 'diag_failed': str(e)}


def clean_numeric_series(series):
    try:
        return pd.to_numeric(series, errors='coerce').replace([float('inf'), -float('inf')], float('nan')).dropna()
    except Exception:
        return pd.Series([])


def normalize_price_df(df):
    try:
        if hasattr(df, 'columns') and isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    except Exception:
        pass
    return df


def get_amp_debug_tail(hist):
    try:
        hist = normalize_price_df(hist)
        cols = [c for c in ['high', 'low', 'close'] if c in hist.columns]
        if not cols:
            return 'missing high/low/close'
        return hist[cols].tail(3).to_dict('records')
    except Exception as e:
        return 'debug_failed:%s' % e


def get_amp_calc_diag(hist, n):
    return calc_amp_stats(hist, n, include_debug=True)


def check_ma10_ma20_both_up_recent20(close):
    if len(close) < 41:
        return False
    for offset in range(0, 20):
        c = close.iloc[: -offset] if offset else close
        if len(c) < 21:
            continue
        ma10 = safe_float(c.iloc[-10:].mean())
        prev_ma10 = safe_float(c.iloc[-11:-1].mean())
        ma20 = safe_float(c.iloc[-20:].mean())
        prev_ma20 = safe_float(c.iloc[-21:-1].mean())
        if ma10 > prev_ma10 and ma20 > prev_ma20:
            return True
    return False


def safe_float(value):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except Exception:
        return 0.0
