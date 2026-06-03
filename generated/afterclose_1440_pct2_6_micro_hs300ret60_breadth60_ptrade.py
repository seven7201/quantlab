# -*- coding: utf-8 -*-
"""
PTrade 策略：afterclose_1440_pct2_6_micro_hs300ret60_breadth60
由 ptrade-quant-lab 生成。

对应本地策略：
strategies/momentum_macd_top5/tmp_40k_variants/40k_afterclose_1440_pct2_6_micro.yaml

核心逻辑：
- 市场开仓过滤：沪深300代理 ret60 > 5%，且全A MA20市场宽度 > 60%，且市场未缩量。
- 选股：涨幅 2%-6%、换手 3%-20%、量比>1.3、站上MA20且MA20上行、近3日MACD金叉、20日均振幅>=4%、近20日MA10/MA20同时上行。
- 排名：按当日涨幅降序，最多保存2只候选；实盘每天最多买1只，总持仓最多6只，单票仓位33%。
- 卖出：跌破MA20；或止损8%；或浮盈20%后，最高价回撤8%且跌破MA10。

注意：PTrade 不同券商版本 API 可能略有差异；首次使用前请在 PTrade 回测环境检查
get_history / get_all_securities / order_value / order_target 参数兼容性。
"""

PARAMS = {
    'stock_pool': [],  # 留空则尝试全A；也可手工填 ['000001.SZ', ...]
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
    'market_weak_pct_chg_lt': -0.5,
}


def initialize(context):
    context.params = PARAMS
    context.today_pool = []
    context.hold_cost = {}
    context.highest_price = {}
    log.info('afterclose_1440_pct2_6_micro_hs300ret60_breadth60 initialized')


def before_trading_start(context, data):
    # 用昨日及以前日线做盘前候选，handle_data 中执行买入。
    regime_ok = is_market_regime_ok(context)
    if not regime_ok:
        context.today_pool = []
        log.info('market regime blocked new positions')
        return
    context.today_pool = select_today_pool(context)
    log.info('today candidates: %s', context.today_pool)


def handle_data(context, data):
    sell_positions(context, data)
    buy_candidates(context, data)


def sell_positions(context, data):
    for stock in list(context.portfolio.positions.keys()):
        position = get_position(stock)
        shares = getattr(position, 'amount', 0) if position else 0
        if shares <= 0:
            continue
        hist = get_history(35, '1d', ['close'], stock, fq='pre')
        if hist is None or len(hist) < 21:
            continue
        close = hist['close']
        current_price = float(close[-1])
        ma20 = float(close[-20:].mean())
        ma10 = float(close[-10:].mean())
        cost = context.hold_cost.get(stock, getattr(position, 'cost_basis', current_price))
        pnl = current_price / cost - 1 if cost else 0.0
        context.highest_price[stock] = max(float(context.highest_price.get(stock, current_price)), current_price)
        high = float(context.highest_price[stock])
        drawdown_from_high = current_price / high - 1 if high else 0.0

        reason = None
        if pnl <= -context.params['stop_loss_pct']:
            reason = 'stop_loss_8pct'
        elif current_price < ma20:
            reason = 'close_below_ma20'
        elif pnl >= context.params['trailing_activation_pct'] and drawdown_from_high <= -context.params['trailing_stop_pct'] and current_price < ma10:
            reason = 'trailing_20_8_below_ma10'

        if reason:
            order_target(stock, 0)
            context.hold_cost.pop(stock, None)
            context.highest_price.pop(stock, None)
            log.info('%s sell: %s', stock, reason)


def buy_candidates(context, data):
    open_positions = [s for s, p in context.portfolio.positions.items() if getattr(p, 'amount', 0) > 0]
    slots = max(context.params['max_positions'] - len(open_positions), 0)
    if slots <= 0:
        return
    bought = 0
    for stock in context.today_pool[:context.params['save_top_n']]:
        if bought >= context.params['max_daily_buys'] or bought >= slots:
            break
        position = get_position(stock)
        if position and getattr(position, 'amount', 0) > 0:
            continue
        target_value = context.portfolio.total_value * context.params['max_position_pct']
        order_value(stock, target_value)
        price = get_last_price(stock, data)
        if price:
            context.hold_cost[stock] = float(price)
            context.highest_price[stock] = float(price)
        bought += 1
        log.info('%s buy candidate', stock)


def select_today_pool(context):
    candidates = []
    for stock in get_candidate_universe(context):
        if is_bj(stock) or is_st(stock):
            continue
        hist = get_history(90, '1d', ['open', 'high', 'low', 'close', 'volume', 'turnover'], stock, fq='pre')
        if hist is None or len(hist) < 61:
            continue
        try:
            close = hist['close']
            volume = hist['volume']
            current = float(close[-1])
            prev = float(close[-2])
            pct_chg = (current / prev - 1) * 100 if prev else 0.0
            ma20 = float(close[-20:].mean())
            prev_ma20 = float(close[-21:-1].mean())
            ma10 = float(close[-10:].mean())
            prev_ma10 = float(close[-11:-1].mean())
            turnover = float(hist['turnover'][-1]) if 'turnover' in hist else 0.0
            vol_ma5 = float(volume[-6:-1].mean())
            volume_ratio = float(volume[-1]) / vol_ma5 if vol_ma5 else 0.0
            avg_amp20 = calc_avg_amplitude_pct(hist, 20)
            dif, dea = calc_macd(close)
            cross_age = macd_cross_age(dif, dea, context.params['macd_cross_within_days'])
            ma10_ma20_both_up_recent20 = check_ma10_ma20_both_up_recent20(close)
        except Exception as e:
            log.info('%s indicator failed: %s', stock, e)
            continue

        if not (context.params['min_pct_chg'] <= pct_chg <= context.params['max_pct_chg']):
            continue
        if not (context.params['min_turnover_pct'] <= turnover <= context.params['max_turnover_pct']):
            continue
        if volume_ratio <= context.params['min_volume_ratio']:
            continue
        if current <= ma20 or ma20 <= prev_ma20:
            continue
        if cross_age is None:
            continue
        if avg_amp20 < context.params['min_avg_amplitude_20d_pct']:
            continue
        if not ma10_ma20_both_up_recent20:
            continue
        candidates.append((stock, pct_chg))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in candidates[:context.params['save_top_n']]]


def is_market_regime_ok(context):
    stocks = get_candidate_universe(context)
    rows = []
    for stock in stocks:
        if is_bj(stock) or is_st(stock):
            continue
        hist = get_history(90, '1d', ['close', 'amount'], stock, fq='pre')
        if hist is None or len(hist) < 61:
            continue
        try:
            close = hist['close']
            amount = hist['amount'] if 'amount' in hist else None
            current = float(close[-1])
            ma20 = float(close[-20:].mean())
            ret60 = current / float(close[-61]) - 1 if float(close[-61]) else 0.0
            amt = float(amount[-1]) if amount is not None else 0.0
            amt_ma20 = float(amount[-20:].mean()) if amount is not None else 0.0
            rows.append({'stock': stock, 'close': current, 'ma20': ma20, 'ret60': ret60, 'amount': amt, 'amount_ma20': amt_ma20})
        except Exception:
            continue
    if not rows:
        return False

    # 全A MA20市场宽度
    valid_breadth = [r for r in rows if r['ma20'] > 0]
    breadth = float(len([r for r in valid_breadth if r['close'] > r['ma20']])) / len(valid_breadth) if valid_breadth else 0.0

    # 沪深300代理：成交额前300的等权 ret60；本地回测用成交额权重代理，PTrade端做可执行近似。
    top300 = sorted(rows, key=lambda r: r['amount'], reverse=True)[:300]
    proxy_ret60 = sum(r['ret60'] for r in top300) / len(top300) if top300 else 0.0

    # 市场缩量过滤：全市场成交额低于20日均额80%则不开仓。
    total_amount = sum(r['amount'] for r in rows)
    total_amount_ma20 = sum(r['amount_ma20'] for r in rows)
    amount_ok = True
    if total_amount_ma20 > 0:
        amount_ok = total_amount >= total_amount_ma20 * context.params['amount_ma20_ratio_min']

    log.info('market regime proxy_ret60=%.2f%% breadth=%.2f%% amount_ok=%s', proxy_ret60 * 100, breadth * 100, amount_ok)
    return proxy_ret60 > context.params['hs300_proxy_ret60_min'] and breadth > context.params['ma20_breadth_min'] and amount_ok


def get_candidate_universe(context):
    if context.params.get('stock_pool'):
        return context.params['stock_pool']
    securities = get_all_securities('stock')
    return list(securities.index)


def is_bj(stock):
    return str(stock).startswith(('8', '4')) or str(stock).endswith('.BJ')


def is_st(stock):
    try:
        info = get_security_info(stock)
        name = getattr(info, 'display_name', '') or getattr(info, 'name', '')
        return 'ST' in str(name).upper() or '*ST' in str(name).upper()
    except Exception:
        return False


def get_last_price(stock, data):
    try:
        if stock in data:
            return data[stock].close
    except Exception:
        pass
    hist = get_history(1, '1d', ['close'], stock, fq='pre')
    if hist is not None and len(hist) > 0:
        return float(hist['close'][-1])
    return None


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def macd_cross_age(dif, dea, max_days):
    # 返回最近一次金叉距今天数；今天金叉为0，昨天为1。
    for age in range(max_days):
        i = -1 - age
        j = -2 - age
        if abs(j) > len(dif):
            break
        if dif[j] <= dea[j] and dif[i] > dea[i]:
            return age
    return None


def calc_avg_amplitude_pct(hist, n):
    if 'high' not in hist or 'low' not in hist or 'close' not in hist or len(hist) < n + 1:
        return 0.0
    vals = []
    for i in range(-n, 0):
        prev_close = float(hist['close'][i - 1])
        if prev_close <= 0:
            continue
        vals.append((float(hist['high'][i]) - float(hist['low'][i])) / prev_close * 100)
    return sum(vals) / len(vals) if vals else 0.0


def check_ma10_ma20_both_up_recent20(close):
    if len(close) < 41:
        return False
    # 近20日内至少有一次 MA10、MA20 同时上行，贴近本地 ma10_ma20_both_up_recent20 特征。
    for offset in range(0, 20):
        end = -offset if offset else None
        c = close[:end]
        if len(c) < 21:
            continue
        ma10 = float(c[-10:].mean())
        prev_ma10 = float(c[-11:-1].mean())
        ma20 = float(c[-20:].mean())
        prev_ma20 = float(c[-21:-1].mean())
        if ma10 > prev_ma10 and ma20 > prev_ma20:
            return True
    return False


def after_trading_end(context, data):
    log.info('afterclose_1440_pct2_6_micro_hs300ret60_breadth60 day end')
