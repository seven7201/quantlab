from __future__ import annotations

import argparse
import csv
import os
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.backtest import BacktestConfig, BacktestResult, DailyBacktester, Position, Trade
from src.codes import infer_market, normalize_code
from src.indicators import add_cross_sectional_ranks, add_indicators
from src.local_data import LocalDailyData
from src.reporting import write_report
from src.spec import load_spec
from src.strategy_factory import build_signal

SRC_2025_ZIP = Path(os.getenv('A_SHARE_MINUTE_2025_ZIP', 'data/minute/2025.zip'))
SRC_2026_ARCHIVES = Path(os.getenv('A_SHARE_MINUTE_2026_DIR', 'data/minute/2026'))
DEFAULT_DAILY_ROOT = Path(os.getenv('A_SHARE_DAILY_DIR', 'data/daily'))
MINUTE_COLUMNS = ['日期', '时间', '开盘', '最高', '最低', '收盘', '成交量', '成交额']


def normalize_code_from_name(name: str) -> str | None:
    stem = Path(name).stem.lower()
    if stem.startswith(('sh', 'sz', 'bj')) and len(stem) >= 8:
        code = stem[2:8]
    elif len(stem) >= 6:
        code = stem[-6:]
    else:
        return None
    return code if code.isdigit() else None


def parse_minute_rows(rows: Iterable[list[str]], code: str, date_filter: str | None = None) -> pd.DataFrame:
    records = []
    for row in rows:
        if len(row) < 8 or row[0] == '日期':
            continue
        date = row[0]
        if date_filter and date != date_filter:
            continue
        try:
            records.append(
                {
                    'code': code,
                    'date': date,
                    'time': str(row[1]).zfill(5),
                    'open': float(row[2]),
                    'high': float(row[3]),
                    'low': float(row[4]),
                    'close': float(row[5]),
                    'volume': float(row[6]),
                    'amount': float(row[7]),
                }
            )
        except (ValueError, IndexError):
            continue
    return pd.DataFrame.from_records(records)


def choose_interval_dir(archive: Path, key: str, requested: str) -> str | None:
    result = subprocess.run(['bsdtar', '-tf', str(archive)], check=True, capture_output=True, text=True)
    entries = set(result.stdout.splitlines())
    intervals = [requested] if requested else []
    intervals.extend([x for x in ('1min', '5min', '15min', '30min', '60min') if x not in intervals])
    for interval in intervals:
        prefix = f'{key}/{interval}/'
        if any(e.startswith(prefix) and e.endswith('.csv') for e in entries):
            return interval
    return None


def load_2025_day(date: str, codes: set[str] | None = None) -> pd.DataFrame:
    if not SRC_2025_ZIP.exists():
        raise FileNotFoundError(SRC_2025_ZIP)
    frames = []
    with zipfile.ZipFile(SRC_2025_ZIP) as z:
        for name in z.namelist():
            if not name.endswith('.csv'):
                continue
            code = normalize_code_from_name(name)
            if not code or (codes and code not in codes):
                continue
            with z.open(name) as raw:
                text = (line.decode('gbk').rstrip('\r\n') for line in raw)
                reader = csv.reader(text)
                next(reader, None)
                df = parse_minute_rows(reader, code, date_filter=date)
                if not df.empty:
                    frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else empty_minute_frame()


def load_2026_day(date: str, requested_interval: str = '1min', codes: set[str] | None = None) -> pd.DataFrame:
    key = date.replace('-', '')
    archive = SRC_2026_ARCHIVES / f'{key}.7z'
    if not archive.exists():
        return empty_minute_frame()
    interval = choose_interval_dir(archive, key, requested_interval)
    if interval is None:
        print(f'skipped {key}: no minute csv directory found')
        return empty_minute_frame()
    frames = []
    with tempfile.TemporaryDirectory(prefix=f'quantlab_min_{key}_') as tmp:
        tmp_path = Path(tmp)
        subprocess.run(['bsdtar', '-xf', str(archive), '-C', str(tmp_path), f'{key}/{interval}'], check=True)
        for csv_path in sorted((tmp_path / key / interval).glob('*.csv')):
            code = normalize_code_from_name(csv_path.name)
            if not code or (codes and code not in codes):
                continue
            for enc in ('utf-8-sig', 'gbk'):
                try:
                    with csv_path.open('r', encoding=enc, newline='') as f:
                        reader = csv.reader(f)
                        next(reader, None)
                        df = parse_minute_rows(reader, code, date_filter=date)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                continue
            if not df.empty:
                frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else empty_minute_frame()
    if not out.empty:
        out['source_interval'] = interval
    return out


def empty_minute_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=['code', 'date', 'time', 'open', 'high', 'low', 'close', 'volume', 'amount'])


def add_minute_indicators(minute: pd.DataFrame, prev_daily: pd.DataFrame, daily_today: pd.DataFrame) -> pd.DataFrame:
    if minute.empty:
        return minute
    day_info = daily_today[['code', 'pre_close', 'turnover_pct', 'market', 'name', 'is_st', 'listed_days']].copy()
    out = minute.merge(day_info, on='code', how='left')
    out['datetime'] = pd.to_datetime(out['date'] + ' ' + out['time'])
    out = out.sort_values(['code', 'datetime']).reset_index(drop=True)
    grouped = out.groupby('code', group_keys=False)
    out['cum_amount'] = grouped['amount'].cumsum()
    out['cum_volume'] = grouped['volume'].cumsum()
    out['turnover_pct'] = out['cum_amount'] / 100_000_000
    out['pct_chg'] = (out['close'] / out['pre_close'] - 1) * 100
    out['market'] = out['market'].fillna(out['code'].map(infer_market))
    out['name'] = out['name'].fillna('')
    out['is_st'] = out['is_st'].fillna(False)
    out['listed_days'] = out['listed_days'].fillna(9999)
    out['paused'] = out[['open', 'high', 'low', 'close']].isna().any(axis=1) | (out['volume'].fillna(0) <= 0)

    seed = prev_daily[['code', 'date', 'close', 'volume', 'pct_chg']].copy()
    seed['datetime'] = pd.to_datetime(seed['date'] + ' 00:00')
    seed['is_seed'] = True
    cur = out[['code', 'date', 'datetime', 'close', 'volume', 'pct_chg']].copy()
    cur['is_seed'] = False
    ind_base = pd.concat([seed, cur], ignore_index=True).sort_values(['code', 'datetime'])
    ind_base = add_indicators(ind_base)
    ind_cols = ['code', 'datetime', 'ma5', 'ma10', 'ma20', 'ma30', 'ma60', 'ret_1d', 'ret_20d', 'dif', 'dea', 'macd', 'prev_dif', 'prev_dea', 'macd_golden_cross']
    out = out.merge(ind_base.loc[~ind_base['is_seed'], ind_cols], on=['code', 'datetime'], how='left')
    out['is_limit_up'] = out['pct_chg'] >= 9.8
    out['is_limit_down'] = out['pct_chg'] <= -9.8
    return out


class MinuteBacktester(DailyBacktester):
    def run(self, signal_func):
        equity_rows = []
        for dt, bar in self.data.groupby('datetime', sort=True):
            date = str(bar.iloc[0]['date'])
            time = str(bar.iloc[0]['time'])
            for _, row in bar.iterrows():
                self.last_prices[str(row['code'])] = float(row[self.config.price_field])

            for _, row in bar.iterrows():
                code = str(row['code'])
                pos = self.positions.get(code)
                signal, reason = signal_func(row, pos)
                if signal == 'SELL' and pos and pos.shares > 0:
                    self._sell_at(f'{date} {time}', row, pos, reason, date)

            buy_candidates = []
            for _, row in bar.iterrows():
                code = str(row['code'])
                pos = self.positions.get(code)
                signal, reason = signal_func(row, pos)
                if signal == 'BUY' and (not pos or pos.shares <= 0):
                    buy_candidates.append((row, reason))
            buy_candidates = self._rank_buy_candidates(buy_candidates)
            if self.config.max_daily_buys is not None:
                buy_candidates = buy_candidates[: self.config.max_daily_buys]
            for row, reason in buy_candidates:
                self._buy_at(f'{date} {time}', row, reason, date)

            equity = self._current_equity()
            equity_rows.append({'date': f'{date} {time}', 'cash': self.cash, 'market_value': equity - self.cash, 'equity': equity})
        return BacktestResult(self.trades, pd.DataFrame(equity_rows), self.cash, self.positions.copy())

    def _buy_at(self, stamp: str, row: pd.Series, reason: str, buy_date: str) -> None:
        if bool(row.get('paused', False)) or self._is_limit_up(row):
            return
        price = float(row[self.config.price_field]) * (1 + self.config.slippage_pct)
        max_value = self._current_equity() * self.config.max_position_pct
        budget = min(self.cash, max_value)
        shares = int(budget // (price * self.config.lot_size)) * self.config.lot_size
        if shares <= 0:
            return
        amount = price * shares
        fee = max(amount * self.config.commission_rate, self.config.min_commission)
        if amount + fee > self.cash:
            return
        self.cash -= amount + fee
        self.positions[str(row['code'])] = Position(shares=shares, avg_cost=price, buy_date=buy_date)
        self.trades.append(Trade(stamp, str(row['code']), 'BUY', price, shares, amount, fee, self.cash, reason))

    def _sell_at(self, stamp: str, row: pd.Series, pos: Position, reason: str, trade_date: str) -> None:
        if pos.buy_date == trade_date:
            return
        if bool(row.get('paused', False)) or self._is_limit_down(row):
            return
        price = float(row[self.config.price_field]) * (1 - self.config.slippage_pct)
        amount = price * pos.shares
        fee = max(amount * self.config.commission_rate, self.config.min_commission) + amount * self.config.stamp_tax_rate
        pnl = (price - pos.avg_cost) * pos.shares - fee
        pnl_pct = price / pos.avg_cost - 1 if pos.avg_cost else 0.0
        self.cash += amount - fee
        self.trades.append(Trade(stamp, str(row['code']), 'SELL', price, pos.shares, amount, fee, self.cash, reason, pnl, pnl_pct))
        self.positions.pop(str(row['code']), None)


def load_minute_range(start: str, end: str, daily_root: Path, codes: set[str] | None, interval: str) -> pd.DataFrame:
    daily_loader = LocalDailyData(daily_root)
    # 多取 90 个自然日用于 MA/MACD 预热；真正回测只保留 start~end 的分钟数据。
    warmup_start = (pd.to_datetime(start) - pd.Timedelta(days=140)).strftime('%Y-%m-%d')
    daily = daily_loader.load_range(warmup_start, end, codes, with_indicators=True)
    days = [d for d in daily_loader.trading_days(start, end) if start <= d <= end]
    frames = []
    for day in days:
        daily_today = daily[daily['date'] == day]
        if daily_today.empty:
            continue
        if day.startswith('2025'):
            minute = load_2025_day(day, codes)
        elif day.startswith('2026'):
            minute = load_2026_day(day, interval, codes)
        else:
            minute = empty_minute_frame()
        if minute.empty:
            print(f'skipped {day}: no minute rows')
            continue
        prev_daily = daily[daily['date'] < day]
        frame = add_minute_indicators(minute, prev_daily, daily_today)
        frames.append(frame)
        print(f'loaded {day}: rows={len(frame)} codes={frame["code"].nunique()}')
    if not frames:
        return empty_minute_frame()
    return add_cross_sectional_ranks(pd.concat(frames, ignore_index=True))


def main() -> None:
    parser = argparse.ArgumentParser(description='Run intraday/minute backtest for local A-share minute archives.')
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--data-root', default=str(DEFAULT_DAILY_ROOT), help='Daily root for calendar, pre_close, filters and warmup indicators.')
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--interval', default='1min')
    parser.add_argument('--report-out', default=None)
    parser.add_argument('--trades-out', default=None)
    parser.add_argument('--equity-out', default=None)
    parser.add_argument('--max-codes', type=int, default=None, help='Debug only: limit universe to first N codes from spec/data.')
    args = parser.parse_args()

    spec = load_spec(args.strategy)
    start = args.start or spec.start
    end = args.end or spec.end
    codes = {normalize_code(c) for c in spec.codes} if spec.codes else None
    if args.max_codes:
        sample = LocalDailyData(Path(args.data_root)).read_day(start)['code'].head(args.max_codes).tolist()
        codes = set(sample) if codes is None else set(list(codes)[: args.max_codes])

    data = load_minute_range(start, end, Path(args.data_root), codes, args.interval)
    config = BacktestConfig(
        initial_cash=spec.initial_cash,
        max_position_pct=spec.max_position_pct,
        max_daily_buys=spec.max_daily_buys,
        buy_rank_field=spec.buy_rank_field,
        buy_rank_ascending=spec.buy_rank_ascending,
    )
    result = MinuteBacktester(data, config).run(build_signal(spec))

    name = spec.name
    report_out = Path(args.report_out or f'reports/{name}_minute_report.md')
    trades_out = Path(args.trades_out or f'generated/{name}_minute_trades.csv')
    equity_out = Path(args.equity_out or f'generated/{name}_minute_equity.csv')
    report_path = write_report(spec, result, report_out)
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    equity_out.parent.mkdir(parents=True, exist_ok=True)
    result.trades_frame().to_csv(trades_out, index=False, encoding='utf-8-sig')
    result.equity_curve.to_csv(equity_out, index=False, encoding='utf-8-sig')
    print('summary:', result.summary())
    print('report:', report_path)
    print('trades:', trades_out)
    print('equity:', equity_out)


if __name__ == '__main__':
    main()
