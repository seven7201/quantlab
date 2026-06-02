from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.codes import infer_market, normalize_code, to_ptrade_code
from src.indicators import add_cross_sectional_ranks, add_indicators
from src.turnover import load_turnover_cache


DEFAULT_DAILY_ROOT = Path(os.getenv('A_SHARE_DAILY_DIR', 'data/daily'))

COLUMN_MAP = {
    '股票代码': 'code',
    '日期': 'date',
    '开盘价': 'open',
    '最高价': 'high',
    '最低价': 'low',
    '收盘价': 'close',
    '昨收价': 'pre_close',
    '涨跌额': 'change',
    '涨跌幅': 'pct_chg',
    '成交量': 'volume',
    '成交额': 'amount',
    '换手率': 'turnover_pct',
    '换手': 'turnover_pct',
    '股票名称': 'name',
    '名称': 'name',
}


@dataclass(frozen=True)
class LocalDailyData:
    root: Path = DEFAULT_DAILY_ROOT

    def available_files(self, start: str | None = None, end: str | None = None) -> list[Path]:
        files = sorted(Path(self.root).glob('*/*.csv'))
        if start:
            start_key = start.replace('-', '')
            files = [p for p in files if p.stem >= start_key]
        if end:
            end_key = end.replace('-', '')
            files = [p for p in files if p.stem <= end_key]
        return files

    def trading_days(self, start: str, end: str) -> list[str]:
        return [f'{p.stem[:4]}-{p.stem[4:6]}-{p.stem[6:]}' for p in self.available_files(start, end)]

    def read_day(self, date: str) -> pd.DataFrame:
        key = date.replace('-', '')
        path = Path(self.root) / key[:4] / f'{key}.csv'
        if not path.exists():
            raise FileNotFoundError(f'日K文件不存在: {path}')
        return normalize_daily_frame(pd.read_csv(path, encoding='utf-8-sig'))

    def load_range(
        self,
        start: str,
        end: str,
        codes: Iterable[str] | None = None,
        with_indicators: bool = True,
        turnover_path: Path | str | None = None,
    ) -> pd.DataFrame:
        wanted = {normalize_code(c) for c in codes} if codes else None
        frames: list[pd.DataFrame] = []
        for path in self.available_files(start, end):
            df = normalize_daily_frame(pd.read_csv(path, encoding='utf-8-sig'))
            if wanted:
                df = df[df['code'].isin(wanted)]
            if not df.empty:
                frames.append(df)
        if not frames:
            return empty_daily_frame()
        out = pd.concat(frames, ignore_index=True).sort_values(['date', 'code']).reset_index(drop=True)
        if turnover_path:
            out = merge_turnover_cache(out, Path(turnover_path))
        if not with_indicators:
            return out
        return add_cross_sectional_ranks(add_indicators(out))

    def load_code(self, code: str, start: str, end: str) -> pd.DataFrame:
        return self.load_range(start, end, [code]).sort_values('date').reset_index(drop=True)

    def sample_codes(self, date: str, limit: int = 100) -> list[str]:
        day = self.read_day(date)
        return day['code'].head(limit).tolist()



def normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns=COLUMN_MAP).copy()
    required = ['code', 'date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'volume', 'amount']
    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(f'日K字段缺失: {missing}')
    keep = required + [c for c in ['turnover_pct', 'name'] if c in out.columns]
    out = out[keep]
    out['code'] = out['code'].map(normalize_code)
    out['date'] = pd.to_datetime(out['date']).dt.strftime('%Y-%m-%d')
    for col in ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'volume', 'amount']:
        out[col] = pd.to_numeric(out[col], errors='coerce')
    if 'turnover_pct' not in out.columns:
        # 当前本地日K只有成交量/成交额，没有流通股本，无法精确还原换手率；
        # 默认先用成交额/1亿元作为可比流动性代理。若提供 turnover cache，会在 load_range 中覆盖为真实换手率。
        out['turnover_pct'] = out['amount'] / 100_000_000
        out['turnover_source'] = 'amount_proxy'
    else:
        out['turnover_pct'] = pd.to_numeric(out['turnover_pct'], errors='coerce')
        out['turnover_source'] = 'local_csv'
    if 'name' not in out.columns:
        out['name'] = ''
    out['market'] = out['code'].map(infer_market)
    out['is_st'] = out['name'].astype(str).str.upper().str.contains('ST', na=False)
    out['paused'] = out[['open', 'high', 'low', 'close']].isna().any(axis=1) | (out['volume'].fillna(0) <= 0)
    return out.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)


def merge_turnover_cache(df: pd.DataFrame, turnover_path: Path) -> pd.DataFrame:
    turnover = load_turnover_cache(turnover_path)
    if turnover.empty:
        return df
    join_cols = ['code', 'date']
    cols = join_cols + ['turnover_pct', 'turnover_source']
    merged = df.merge(turnover[cols], on=join_cols, how='left', suffixes=('', '_real'))
    has_real = merged['turnover_pct_real'].notna()
    merged.loc[has_real, 'turnover_pct'] = merged.loc[has_real, 'turnover_pct_real']
    merged.loc[has_real, 'turnover_source'] = merged.loc[has_real, 'turnover_source_real'].fillna('cache')
    return merged.drop(columns=['turnover_pct_real', 'turnover_source_real'])


def empty_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            'code',
            'date',
            'open',
            'high',
            'low',
            'close',
            'pre_close',
            'pct_chg',
            'volume',
            'amount',
            'turnover_pct',
            'turnover_source',
            'market',
            'name',
            'is_st',
            'paused',
        ]
    )
