from __future__ import annotations

import pandas as pd


def add_indicators(df: pd.DataFrame, windows: tuple[int, ...] = (5, 10, 20, 30, 60)) -> pd.DataFrame:
    out = df.sort_values(['code', 'date']).copy()
    grouped = out.groupby('code', group_keys=False)
    for n in windows:
        out[f'ma{n}'] = grouped['close'].transform(lambda s: s.rolling(n, min_periods=n).mean())
        out[f'vol_ma{n}'] = grouped['volume'].transform(lambda s: s.rolling(n, min_periods=n).mean())
        out[f'prev_ma{n}'] = grouped[f'ma{n}'].shift(1)
    out['prev_close'] = grouped['close'].shift(1)
    out['ret_1d'] = grouped['close'].pct_change()
    out['ret_20d'] = grouped['close'].pct_change(20)
    out['listed_days'] = grouped.cumcount() + 1
    out['is_new_60d'] = out['listed_days'] <= 60
    out['is_limit_up'] = out['pct_chg'] >= 9.8
    out['is_limit_down'] = out['pct_chg'] <= -9.8
    out = add_macd(out)
    return out


def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    out = df.sort_values(['code', 'date']).copy()
    grouped = out.groupby('code', group_keys=False)
    ema_fast = grouped['close'].transform(lambda s: s.ewm(span=fast, adjust=False, min_periods=fast).mean())
    ema_slow = grouped['close'].transform(lambda s: s.ewm(span=slow, adjust=False, min_periods=slow).mean())
    out['dif'] = ema_fast - ema_slow
    out['dea'] = grouped['dif'].transform(lambda s: s.ewm(span=signal, adjust=False, min_periods=signal).mean())
    out['macd'] = (out['dif'] - out['dea']) * 2
    out['prev_dif'] = grouped['dif'].shift(1)
    out['prev_dea'] = grouped['dea'].shift(1)
    out['macd_golden_cross'] = (
        out['prev_dif'].notna()
        & out['prev_dea'].notna()
        & (out['prev_dif'] <= out['prev_dea'])
        & (out['dif'] > out['dea'])
    )
    return out


def add_cross_sectional_ranks(
    df: pd.DataFrame,
    return_col: str = 'ret_20d',
    rank_col: str = 'ret_20d_rank_pct',
    ascending: bool = False,
) -> pd.DataFrame:
    """Add per-date percentile rank. Best return gets percentile close to 1.0 by default."""
    out = df.copy()
    out[rank_col] = out.groupby('date')[return_col].rank(pct=True, ascending=ascending)
    return out


def crossed_above(row: pd.Series, fast: str, slow: str) -> bool:
    return (
        pd.notna(row.get(fast))
        and pd.notna(row.get(slow))
        and pd.notna(row.get(f'prev_{fast}'))
        and pd.notna(row.get(f'prev_{slow}'))
        and float(row[f'prev_{fast}']) <= float(row[f'prev_{slow}'])
        and float(row[fast]) > float(row[slow])
    )


def crossed_below(row: pd.Series, fast: str, slow: str) -> bool:
    return (
        pd.notna(row.get(fast))
        and pd.notna(row.get(slow))
        and pd.notna(row.get(f'prev_{fast}'))
        and pd.notna(row.get(f'prev_{slow}'))
        and float(row[f'prev_{fast}']) >= float(row[f'prev_{slow}'])
        and float(row[fast]) < float(row[slow])
    )
