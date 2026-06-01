from __future__ import annotations

from pathlib import Path

from src.codes import to_ptrade_code
from src.spec import StrategySpec

MA_VOLUME_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
{strategy_name}
由 ptrade-quant-lab 生成。
复制到国金证券 PTrade 前，请先在 PTrade 回测环境验证 get_history/order_* API 参数。
"""

PARAMS = {params!r}


def initialize(context):
    context.params = PARAMS
    context.hold_cost = {{}}
    log.info('{strategy_name} initialized')


def before_trading_start(context, data):
    context.today_pool = context.params['stock_pool']


def handle_data(context, data):
    for stock in context.today_pool:
        hist = get_history(25, '1d', ['close', 'volume'], stock, fq='pre')
        if hist is None or len(hist) < 21:
            continue

        close = hist['close']
        volume = hist['volume']
        ma5 = close[-5:].mean()
        ma10 = close[-10:].mean()
        ma20 = close[-20:].mean()
        prev_ma5 = close[-6:-1].mean()
        prev_ma20 = close[-21:-1].mean()
        vol_ma5 = volume[-5:].mean()
        current_price = close[-1]

        position = get_position(stock)
        shares = getattr(position, 'amount', 0) if position else 0

        if shares > 0:
            cost = context.hold_cost.get(stock, getattr(position, 'cost_basis', current_price))
            pnl = current_price / cost - 1 if cost else 0
            if pnl <= -context.params['stop_loss_pct']:
                order_target(stock, 0)
                log.info('%s stop loss sell', stock)
            elif pnl >= context.params['take_profit_pct']:
                order_target(stock, 0)
                log.info('%s take profit sell', stock)
            elif current_price < ma10:
                order_target(stock, 0)
                log.info('%s close below ma10 sell', stock)
            continue

        cross_up = prev_ma5 <= prev_ma20 and ma5 > ma20
        volume_ok = volume[-1] > vol_ma5 * context.params['volume_ratio']
        trend_ok = current_price > ma20
        if cross_up and volume_ok and trend_ok:
            portfolio_value = context.portfolio.total_value
            target_value = portfolio_value * context.params['max_position_pct']
            order_value(stock, target_value)
            context.hold_cost[stock] = current_price
            log.info('%s buy signal: ma5 cross ma20 with volume', stock)


def after_trading_end(context, data):
    log.info('{strategy_name} day end')
'''

MOMENTUM_MACD_TOP5_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
{strategy_name}
由 ptrade-quant-lab 生成。
规则：排除ST、北交所、上市60天以内新股；20日涨幅前10%；换手率>3%；站上20日均线；MACD金叉；仅买排名前5。
注意：PTrade不同券商版本的 API 名称可能不同，get_all_securities/get_fundamentals/get_history/order_value 参数需在平台回测环境校验。
"""

PARAMS = {params!r}


def initialize(context):
    context.params = PARAMS
    context.hold_cost = {{}}
    log.info('{strategy_name} initialized')


def before_trading_start(context, data):
    context.today_pool = select_today_pool(context)
    log.info('today selected %s stocks: %s', len(context.today_pool), context.today_pool)


def handle_data(context, data):
    # 先处理卖出
    for stock in list(context.portfolio.positions.keys()):
        hist = get_history(35, '1d', ['close'], stock, fq='pre')
        if hist is None or len(hist) < 21:
            continue
        close = hist['close']
        current_price = close[-1]
        ma20 = close[-20:].mean()
        dif, dea = calc_macd(close)
        cost = context.hold_cost.get(stock, getattr(context.portfolio.positions[stock], 'cost_basis', current_price))
        pnl = current_price / cost - 1 if cost else 0
        if pnl <= -context.params['stop_loss_pct']:
            order_target(stock, 0)
            log.info('%s stop loss sell', stock)
        elif pnl >= context.params['take_profit_pct']:
            order_target(stock, 0)
            log.info('%s take profit sell', stock)
        elif current_price < ma20:
            order_target(stock, 0)
            log.info('%s close below ma20 sell', stock)
        elif dif[-1] < dea[-1]:
            order_target(stock, 0)
            log.info('%s macd weak sell', stock)

    # 再按排名买入前5名
    for stock in context.today_pool[:context.params['max_daily_buys']]:
        position = get_position(stock)
        shares = getattr(position, 'amount', 0) if position else 0
        if shares > 0:
            continue
        target_value = context.portfolio.total_value * context.params['max_position_pct']
        order_value(stock, target_value)
        price = data[stock].close if stock in data else None
        if price:
            context.hold_cost[stock] = price
        log.info('%s buy top momentum macd candidate', stock)


def select_today_pool(context):
    candidates = []
    stocks = get_candidate_universe(context)
    for stock in stocks:
        if is_bj(stock) or is_st(stock) or is_new_stock(stock, context.params['min_listed_days']):
            continue
        hist = get_history(35, '1d', ['close', 'turnover'], stock, fq='pre')
        if hist is None or len(hist) < 26:
            continue
        close = hist['close']
        current_price = close[-1]
        ma20 = close[-20:].mean()
        ret20 = current_price / close[-21] - 1
        turnover = hist['turnover'][-1] if 'turnover' in hist else 0
        dif, dea = calc_macd(close)
        macd_cross = dif[-2] <= dea[-2] and dif[-1] > dea[-1]
        if turnover > context.params['min_turnover_pct'] and current_price > ma20 and macd_cross:
            candidates.append((stock, ret20))

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_n = max(int(len(candidates) * context.params['return_top_pct']), context.params['max_daily_buys'])
    return [s for s, _ in candidates[:top_n]][:context.params['max_daily_buys']]


def get_candidate_universe(context):
    # 优先用外部传入股票池；为空则尝试全A股。不同PTrade版本可能需要替换为平台对应API。
    if context.params.get('stock_pool'):
        return context.params['stock_pool']
    securities = get_all_securities('stock')
    return list(securities.index)


def is_bj(stock):
    return stock.startswith('8') or stock.startswith('4') or stock.endswith('.BJ')


def is_st(stock):
    try:
        info = get_security_info(stock)
        name = getattr(info, 'display_name', '') or getattr(info, 'name', '')
        return 'ST' in name.upper()
    except Exception:
        return False


def is_new_stock(stock, min_days):
    try:
        info = get_security_info(stock)
        # 若平台返回 start_date/list_date，为 datetime/date 均可相减；否则保守不按新股剔除。
        list_date = getattr(info, 'start_date', None) or getattr(info, 'list_date', None)
        if not list_date:
            return False
        return (get_datetime().date() - list_date).days < min_days
    except Exception:
        return False


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def after_trading_end(context, data):
    log.info('{strategy_name} day end')
'''


def generate_ptrade_code(spec: StrategySpec) -> str:
    if str(spec.raw.get('strategy_type', spec.raw.get('type', ''))) == 'momentum_macd_top5':
        return _generate_momentum_macd_top5(spec)
    return _generate_ma_volume(spec)


def _generate_ma_volume(spec: StrategySpec) -> str:
    params = {
        'stock_pool': [to_ptrade_code(c) for c in spec.codes],
        'max_position_pct': spec.max_position_pct,
        'stop_loss_pct': spec.stop_loss_pct,
        'take_profit_pct': spec.take_profit_pct,
        'volume_ratio': float(spec.raw.get('buy', {}).get('volume_ratio', 1.2)),
    }
    return MA_VOLUME_TEMPLATE.format(strategy_name=spec.name, params=params)


def _generate_momentum_macd_top5(spec: StrategySpec) -> str:
    buy = spec.raw.get('buy', {})
    filters = spec.raw.get('filters', {})
    params = {
        'stock_pool': [to_ptrade_code(c) for c in spec.codes],
        'max_position_pct': spec.max_position_pct,
        'stop_loss_pct': spec.stop_loss_pct,
        'take_profit_pct': spec.take_profit_pct,
        'return_top_pct': float(buy.get('return_top_pct', 0.10)),
        'min_turnover_pct': float(buy.get('min_turnover_pct', 3.0)),
        'max_daily_buys': int(buy.get('max_daily_buys', spec.max_daily_buys or 5)),
        'min_listed_days': int(filters.get('min_listed_days', 61)),
    }
    return MOMENTUM_MACD_TOP5_TEMPLATE.format(strategy_name=spec.name, params=params)


def write_ptrade_code(spec: StrategySpec, out: str | Path) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_ptrade_code(spec), encoding='utf-8')
    return path
