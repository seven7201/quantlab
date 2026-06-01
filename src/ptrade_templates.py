PTRADE_TEMPLATE = r'''# -*- coding: utf-8 -*-
"""
国金证券 PTrade 示例策略：MA5 上穿 MA20 + 放量买入，跌破 MA10/止损/止盈卖出。
由 ptrade-quant-lab 生成。复制到 PTrade 前请按平台实际 API 名称微调。
"""

PARAMS = {
    'stock_pool': ['000001.SZ', '000002.SZ', '000006.SZ'],
    'max_position_pct': 0.20,
    'stop_loss_pct': 0.08,
    'take_profit_pct': 0.15,
    'volume_ratio': 1.20,
}


def initialize(context):
    context.params = PARAMS
    context.hold_cost = {}
    log.info('MA volume strategy initialized')


def before_trading_start(context, data):
    # 日线策略：盘前可做股票池过滤；MVP 先使用固定股票池。
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
            pnl = current_price / cost - 1
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
    log.info('strategy day end')
'''
