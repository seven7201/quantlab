from __future__ import annotations

import pandas as pd

from src.backtest import Position


def ma_volume_signal(row: pd.Series, pos: Position | None) -> tuple[str | None, str]:
    close = float(row['close'])
    ma5 = row.get('ma5')
    ma10 = row.get('ma10')
    ma20 = row.get('ma20')
    prev_ma5 = row.get('prev_ma5')
    prev_ma20 = row.get('prev_ma20')
    vol_ma5 = row.get('vol_ma5')

    if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20) or pd.isna(prev_ma5) or pd.isna(prev_ma20):
        return None, 'not_enough_data'

    if pos and pos.shares > 0:
        pnl = close / pos.avg_cost - 1
        if pnl <= -0.08:
            return 'SELL', 'stop_loss'
        if pnl >= 0.15:
            return 'SELL', 'take_profit'
        if close < float(ma10):
            return 'SELL', 'sell_signal'
        return None, 'hold'

    cross_up = float(prev_ma5) <= float(prev_ma20) and float(ma5) > float(ma20)
    volume_ok = not pd.isna(vol_ma5) and float(row['volume']) > float(vol_ma5) * 1.2
    trend_ok = close > float(ma20)
    not_limit_up = float(row.get('pct_chg', 0) or 0) < 9.8
    if cross_up and volume_ok and trend_ok and not_limit_up:
        return 'BUY', 'ma5_cross_ma20_volume'
    return None, 'no_signal'
