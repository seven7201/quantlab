from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd

from src.backtest import BacktestConfig, BacktestResult, DailyBacktester, Position, Trade
from src.codes import normalize_code
from src.local_data import LocalDailyData
from src.reporting import write_report
from src.spec import load_spec
from src.strategy_factory import build_signal
from tools.run_minute_backtest import empty_minute_frame, load_2025_day, load_2026_day


def add_market_weak_flag(daily: pd.DataFrame, spec) -> pd.DataFrame:
    """Add a same-day full-market breadth proxy for weak market filtering.

    The local dataset does not currently include index bars, so this uses the
    tradeable A-share universe as a market proxy. Defaults are intentionally
    mild: pause new buys when fewer than 45% of stocks are up and median return
    is negative.
    """
    buy = spec.raw.get('buy', {})
    market_cfg = buy.get('market_weak', {}) if isinstance(buy.get('market_weak', {}), dict) else {}
    up_ratio_threshold = float(market_cfg.get('up_ratio_lt', buy.get('market_up_ratio_lt', 0.45)))
    median_pct_threshold = float(market_cfg.get('median_pct_lt', buy.get('market_median_pct_lt', 0.0)))
    out = daily.copy()
    breadth = out.groupby('date').agg(
        market_up_ratio=('pct_chg', lambda s: float((s > 0).mean())),
        market_median_pct=('pct_chg', 'median'),
    ).reset_index()
    breadth['market_weak'] = (breadth['market_up_ratio'] < up_ratio_threshold) & (breadth['market_median_pct'] < median_pct_threshold)
    return out.merge(breadth, on='date', how='left')


class DailySignalMinuteExecutor(DailyBacktester):
    """Use daily bars for signal generation and minute bars only for execution.

    Execution model:
    - Daily BUY/SELL signals are generated after day close and become pending orders.
    - Pending SELL orders execute on the next trading day's first available minute.
    - Pending BUY orders execute on the next trading day's first available minute after sells.
    - Intraday stop-loss / take-profit checks run on held positions using minute close.
    - A-share T+1 is enforced for all sells.
    """

    def __init__(self, daily: pd.DataFrame, daily_root: Path, interval: str, config: BacktestConfig):
        super().__init__(daily, config)
        self.daily_root = daily_root
        self.interval = interval
        self.daily_by_date = {d: x.copy() for d, x in self.data.groupby('date', sort=True)}
        self.daily_by_code_date = {(str(r.code), str(r.date)): r for r in self.data.itertuples(index=False)}
        self.stop_loss_pct = 0.08
        self.take_profit_pct = 0.20
        self.pending_buys: list[tuple[pd.Series, str]] = []
        self.pending_sells: dict[str, str] = {}
        self.minute_intervals: dict[str, str] = {}

    def run(self, signal_func):
        equity_rows = []
        for date, day in self.data.groupby('date', sort=True):
            date = str(date)
            # Daily close is used for mark-to-market if no minute price loaded yet.
            for _, row in day.iterrows():
                self.last_prices[str(row['code'])] = float(row[self.config.price_field])

            needed_codes = set(self.positions) | {str(row['code']) for row, _ in self.pending_buys} | set(self.pending_sells)
            minute = self._load_needed_minutes(date, needed_codes)
            if not minute.empty:
                self._execute_pending_at_first_minutes(date, minute)
                self._process_intraday_risk(date, minute)
                for code, grp in minute.groupby('code'):
                    self.last_prices[str(code)] = float(grp.iloc[-1][self.config.price_field])

            # If no minute data is available, fall back to daily close execution for pending orders.
            if minute.empty and (self.pending_sells or self.pending_buys):
                self._execute_pending_at_daily_close(date, day)

            equity = self._current_equity()
            equity_rows.append({'date': date, 'cash': self.cash, 'market_value': equity - self.cash, 'equity': equity})

            # Generate orders for next trading day from today's daily signal.
            next_sells: dict[str, str] = {}
            buy_candidates: list[tuple[pd.Series, str]] = []
            for _, row in day.iterrows():
                code = str(row['code'])
                pos = self.positions.get(code)
                signal, reason = signal_func(row, pos)
                if signal == 'SELL' and pos and pos.shares > 0:
                    next_sells[code] = reason
                elif signal == 'BUY' and (not pos or pos.shares <= 0):
                    buy_candidates.append((row, reason))
            buy_candidates = self._rank_buy_candidates(buy_candidates)
            max_daily_buys = self.config.max_daily_buys
            if bool(day['market_weak'].fillna(False).any()) and self.config.weak_market_max_daily_buys is not None:
                max_daily_buys = self.config.weak_market_max_daily_buys
            if max_daily_buys is not None:
                buy_candidates = buy_candidates[:max_daily_buys]
            self.pending_sells = next_sells
            self.pending_buys = buy_candidates

        return BacktestResult(self.trades, pd.DataFrame(equity_rows), self.cash, self.positions.copy())

    def _load_needed_minutes(self, date: str, codes: set[str]) -> pd.DataFrame:
        if not codes:
            return empty_minute_frame()
        if date.startswith('2025'):
            minute = load_2025_day(date, codes)
        elif date.startswith('2026'):
            minute = load_2026_day(date, self.interval, codes)
            if not minute.empty and 'source_interval' in minute.columns:
                self.minute_intervals[date] = str(minute['source_interval'].dropna().iloc[0])
        else:
            minute = empty_minute_frame()
        if minute.empty:
            return minute
        minute['datetime'] = pd.to_datetime(minute['date'] + ' ' + minute['time'])
        daily_today = self.daily_by_date.get(date)
        if daily_today is not None:
            cols = ['code', 'pre_close', 'pct_chg', 'is_limit_up', 'is_limit_down', 'paused']
            minute = minute.merge(daily_today[[c for c in cols if c in daily_today.columns]], on='code', how='left', suffixes=('', '_daily'))
        return minute.sort_values(['datetime', 'code']).reset_index(drop=True)

    def _execute_pending_at_first_minutes(self, date: str, minute: pd.DataFrame) -> None:
        if self.pending_sells:
            first_by_code = minute.sort_values('datetime').groupby('code', as_index=False).first()
            for _, row in first_by_code.iterrows():
                code = str(row['code'])
                reason = self.pending_sells.get(code)
                pos = self.positions.get(code)
                if reason and pos and pos.shares > 0:
                    self._sell_at(f'{date} {row["time"]}', row, pos, f'next_open_{reason}', date)
            self.pending_sells = {}

        if self.pending_buys:
            first_by_code = {str(r['code']): r for _, r in minute.sort_values('datetime').groupby('code', as_index=False).first().iterrows()}
            for daily_row, reason in self.pending_buys:
                code = str(daily_row['code'])
                row = first_by_code.get(code)
                if row is not None:
                    enriched = row.copy()
                    for col, val in daily_row.items():
                        if col not in enriched.index or pd.isna(enriched.get(col)):
                            enriched[col] = val
                    self._buy_at(f'{date} {enriched["time"]}', enriched, f'next_open_{reason}', date)
            self.pending_buys = []

    def _process_intraday_risk(self, date: str, minute: pd.DataFrame) -> None:
        if not self.positions:
            return
        for _, row in minute.iterrows():
            code = str(row['code'])
            pos = self.positions.get(code)
            if not pos or pos.shares <= 0 or pos.buy_date == date:
                continue
            price = float(row[self.config.price_field])
            pnl = price / pos.avg_cost - 1 if pos.avg_cost else 0.0
            if pnl <= -self.stop_loss_pct:
                self._sell_at(f'{date} {row["time"]}', row, pos, 'intraday_stop_loss', date)
            elif pnl >= self.take_profit_pct:
                self._sell_at(f'{date} {row["time"]}', row, pos, 'intraday_take_profit', date)

    def _execute_pending_at_daily_close(self, date: str, day: pd.DataFrame) -> None:
        rows = {str(r['code']): r for _, r in day.iterrows()}
        for code, reason in list(self.pending_sells.items()):
            pos = self.positions.get(code)
            row = rows.get(code)
            if pos and row is not None:
                self._sell_at(date, row, pos, f'daily_fallback_{reason}', date)
        self.pending_sells = {}
        for row, reason in self.pending_buys:
            self._buy_at(date, row, f'daily_fallback_{reason}', date)
        self.pending_buys = []

    def _buy_at(self, stamp: str, row: pd.Series, reason: str, buy_date: str) -> None:
        if bool(row.get('paused', False)) or self._is_limit_up(row):
            return
        price = float(row[self.config.price_field]) * (1 + self.config.slippage_pct)
        max_position_pct = self.config.max_position_pct
        if bool(row.get('market_weak', False)) and self.config.weak_market_max_position_pct is not None:
            max_position_pct = self.config.weak_market_max_position_pct
        max_value = self._current_equity() * max_position_pct
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


def main() -> None:
    parser = argparse.ArgumentParser(description='Run fast hybrid backtest: daily signals, minute execution.')
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--data-root', default=os.getenv('A_SHARE_DAILY_DIR', 'data/daily'))
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--interval', default='1min')
    parser.add_argument('--report-out', default=None)
    parser.add_argument('--trades-out', default=None)
    parser.add_argument('--equity-out', default=None)
    parser.add_argument('--turnover-path', default=None)
    args = parser.parse_args()

    spec = load_spec(args.strategy)
    start = args.start or spec.start
    end = args.end or spec.end
    codes = {normalize_code(c) for c in spec.codes} if spec.codes else None
    daily = LocalDailyData(Path(args.data_root)).load_range(start, end, codes, with_indicators=True, turnover_path=args.turnover_path)
    daily = add_market_weak_flag(daily, spec)
    market_weak_cfg = spec.raw.get('buy', {}).get('market_weak', {})
    if not isinstance(market_weak_cfg, dict):
        market_weak_cfg = {}
    config = BacktestConfig(
        initial_cash=spec.initial_cash,
        max_position_pct=spec.max_position_pct,
        max_daily_buys=spec.max_daily_buys,
        buy_rank_field=spec.buy_rank_field,
        buy_rank_ascending=spec.buy_rank_ascending,
        weak_market_max_position_pct=market_weak_cfg.get('max_position_pct'),
        weak_market_max_daily_buys=market_weak_cfg.get('max_daily_buys'),
    )
    executor = DailySignalMinuteExecutor(daily, Path(args.data_root), args.interval, config)
    executor.stop_loss_pct = spec.stop_loss_pct
    executor.take_profit_pct = spec.take_profit_pct
    result = executor.run(build_signal(spec))

    name = spec.name
    report_out = Path(args.report_out or f'reports/{name}_daily_signal_minute_exec_report.md')
    trades_out = Path(args.trades_out or f'generated/{name}_daily_signal_minute_exec_trades.csv')
    equity_out = Path(args.equity_out or f'generated/{name}_daily_signal_minute_exec_equity.csv')
    report_path = write_report(spec, result, report_out)
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    equity_out.parent.mkdir(parents=True, exist_ok=True)
    result.trades_frame().to_csv(trades_out, index=False, encoding='utf-8-sig')
    result.equity_curve.to_csv(equity_out, index=False, encoding='utf-8-sig')

    print('summary:', result.summary())
    print('report:', report_path)
    print('trades:', trades_out)
    print('equity:', equity_out)
    print('minute_intervals:', executor.minute_intervals)


if __name__ == '__main__':
    main()
