from __future__ import annotations

import math

import pandas as pd

from src.backtest import Position, SignalFunc
from src.indicators import crossed_above
from src.spec import StrategySpec


def build_signal(spec: StrategySpec) -> SignalFunc:
    strategy_type = str(spec.raw.get('type', spec.raw.get('strategy_type', 'ma_volume_breakout')))
    if strategy_type == 'momentum_macd_top5':
        return build_momentum_macd_top5_signal(spec)
    return build_ma_volume_signal(spec)


def build_ma_volume_signal(spec: StrategySpec) -> SignalFunc:
    buy = spec.raw.get('buy', {})
    sell = spec.raw.get('sell', {})
    risk = spec.raw.get('risk', {})

    fast = str(buy.get('cross_fast', 'ma5'))
    slow = str(buy.get('cross_slow', 'ma20'))
    trend_ma = str(buy.get('trend_ma', slow))
    volume_window = int(buy.get('volume_window', 5))
    volume_ratio = float(buy.get('volume_ratio', 1.2))
    sell_ma = str(sell.get('below_ma', 'ma10'))
    stop_loss_pct = float(risk.get('stop_loss_pct', 0.08))
    take_profit_pct = float(risk.get('take_profit_pct', 0.15))

    def signal(row: pd.Series, pos: Position | None) -> tuple[str | None, str]:
        close = float(row['close'])
        if pos and pos.shares > 0:
            pnl = close / pos.avg_cost - 1 if pos.avg_cost else 0.0
            if pnl <= -stop_loss_pct:
                return 'SELL', 'stop_loss'
            if pnl >= take_profit_pct:
                return 'SELL', 'take_profit'
            ma_value = row.get(sell_ma)
            if pd.notna(ma_value) and close < float(ma_value):
                return 'SELL', f'close_below_{sell_ma}'
            return None, 'hold'

        needed = [fast, slow, trend_ma, f'vol_ma{volume_window}']
        if any(pd.isna(row.get(x)) for x in needed):
            return None, 'not_enough_data'
        cross_ok = crossed_above(row, fast, slow)
        trend_ok = close > float(row[trend_ma])
        volume_ok = float(row['volume']) > float(row[f'vol_ma{volume_window}']) * volume_ratio
        limit_ok = not bool(row.get('is_limit_up', False))
        if cross_ok and trend_ok and volume_ok and limit_ok:
            return 'BUY', f'{fast}_cross_{slow}_volume'
        return None, 'no_signal'

    return signal


def build_momentum_macd_top5_signal(spec: StrategySpec) -> SignalFunc:
    buy = spec.raw.get('buy', {})
    sell = spec.raw.get('sell', {})
    filters = spec.raw.get('filters', {})
    risk = spec.raw.get('risk', {})

    return_window = int(buy.get('return_window', 20))
    top_pct = float(buy.get('return_top_pct', 0.10))
    min_turnover = float(buy.get('min_turnover_pct', 3.0))
    trend_ma = str(buy.get('trend_ma', 'ma20'))
    require_macd_golden_cross = bool(buy.get('macd_golden_cross', True))
    sell_ma = str(sell.get('below_ma', 'ma20'))
    stop_loss_pct = float(risk.get('stop_loss_pct', 0.08))
    take_profit_pct = float(risk.get('take_profit_pct', 0.20))
    trailing_stop_pct = risk.get('trailing_stop_pct')
    trailing_stop_pct = float(trailing_stop_pct) if trailing_stop_pct is not None else None

    def signal(row: pd.Series, pos: Position | None) -> tuple[str | None, str]:
        close = float(row['close'])
        if pos and pos.shares > 0:
            pnl = close / pos.avg_cost - 1 if pos.avg_cost else 0.0
            if pnl <= -stop_loss_pct:
                return 'SELL', 'stop_loss'
            if pnl >= take_profit_pct:
                return 'SELL', 'take_profit'
            if trailing_stop_pct is not None:
                max_close = row.get('max_close_since_entry')
                if pd.notna(max_close) and close / float(max_close) - 1 <= -trailing_stop_pct:
                    return 'SELL', 'trailing_stop'
            ma_value = row.get(sell_ma)
            if pd.notna(ma_value) and close < float(ma_value):
                return 'SELL', f'close_below_{sell_ma}'
            if pd.notna(row.get('dif')) and pd.notna(row.get('dea')) and float(row['dif']) < float(row['dea']):
                return 'SELL', 'macd_dead_or_weak'
            return None, 'hold'

        if not _passes_universe_filters(row, filters):
            return None, 'filtered_universe'
        needed = [f'ret_{return_window}d', f'ret_{return_window}d_rank_pct', trend_ma, 'turnover_pct', 'dif', 'dea']
        if any(pd.isna(row.get(x)) for x in needed):
            return None, 'not_enough_data'
        rank_ok = float(row[f'ret_{return_window}d_rank_pct']) <= top_pct
        turnover_ok = float(row['turnover_pct']) > min_turnover
        trend_ok = close > float(row[trend_ma])
        macd_ok = bool(row.get('macd_golden_cross', False)) if require_macd_golden_cross else float(row['dif']) > float(row['dea'])
        limit_ok = not bool(row.get('is_limit_up', False))
        if rank_ok and turnover_ok and trend_ok and macd_ok and limit_ok:
            ret = float(row[f'ret_{return_window}d'])
            return 'BUY', f'top{int(top_pct * 100)}pct_ret{ret:.2%}_turnover_macd'
        return None, 'no_signal'

    return signal


def _passes_universe_filters(row: pd.Series, filters: dict) -> bool:
    code = str(row.get('code', ''))
    if bool(filters.get('exclude_bj', True)) and (code.startswith(('8', '4')) or str(row.get('market', '')).upper() == 'BJ'):
        return False
    if bool(filters.get('exclude_st', True)) and bool(row.get('is_st', False)):
        return False
    min_listed_days = int(filters.get('min_listed_days', 61))
    listed_days = row.get('listed_days')
    if pd.isna(listed_days) or int(listed_days) < min_listed_days:
        return False
    return True
