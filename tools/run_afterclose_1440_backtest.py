from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
import yaml

from src.backtest import BacktestConfig, Position, Trade
from src.indicators import add_cross_sectional_ranks, add_indicators
from src.local_data import DEFAULT_DAILY_ROOT, LocalDailyData
from src.reporting import write_report
from src.spec import StrategySpec, load_spec


@dataclass
class Candidate:
    signal_date: str
    buy_date: str
    code: str
    rank: int
    pct_chg: float
    turnover_pct: float
    volume_ratio: float
    close: float
    ma20: float
    reason: str


class AfterCloseNextDayBacktester:
    def __init__(self, data: pd.DataFrame, spec: StrategySpec):
        self.data = data.sort_values(['date', 'code']).reset_index(drop=True)
        self.spec = spec
        self.buy_cfg = spec.raw.get('buy', {})
        self.sell_cfg = spec.raw.get('sell', {})
        self.risk_cfg = spec.raw.get('risk', {})
        self.market_cfg = spec.raw.get('market_regime', {})
        self.max_positions = int(self.risk_cfg.get('max_positions', 3))
        self.config = BacktestConfig(
            initial_cash=spec.initial_cash,
            max_position_pct=float(self.risk_cfg.get('max_position_pct', 0.33)),
            max_daily_buys=int(self.risk_cfg.get('max_daily_buys', 1)),
        )
        self.cash = self.config.initial_cash
        self.positions: dict[str, Position] = {}
        self.entry_high: dict[str, float] = {}
        self.last_prices: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.candidates: list[Candidate] = []
        self.pending_sells: dict[str, str] = {}
        self.next_day_candidates: dict[str, list[Candidate]] = {}
        self.market_by_date = self._market_regime_by_date()

    def run(self):
        equity_rows = []
        days = sorted(self.data['date'].unique())
        for idx, date in enumerate(days):
            day = self.data[self.data['date'] == date]
            for _, row in day.iterrows():
                self.last_prices[str(row['code'])] = float(row['close'])

            # 次日执行前一日收盘后的卖出纪律：跌破MA20 / MACD死叉 / 移动止盈。
            for code, reason in list(self.pending_sells.items()):
                row_df = day[day['code'] == code]
                pos = self.positions.get(code)
                if pos and not row_df.empty:
                    self._sell(date, row_df.iloc[0], pos, f'next_day_{reason}')
                self.pending_sells.pop(code, None)

            # 盘中14:40：只复核昨日盘后候选，仍满足条件才按仓位上限买入。
            buys_today = 0
            for cand in self.next_day_candidates.get(date, []):
                if buys_today >= self.config.max_daily_buys:
                    break
                if self._open_position_count() >= self.max_positions:
                    break
                if cand.code in self.positions and self.positions[cand.code].shares > 0:
                    continue
                row_df = day[day['code'] == cand.code]
                if row_df.empty:
                    continue
                row = row_df.iloc[0]
                ok, reason = self._intraday_recheck(row)
                if ok:
                    self._buy(date + ' 14:40', row, f'14:40_recheck_ok_from_{cand.signal_date}_{reason}')
                    buys_today += 1

            # 收盘后持仓监控：计算移动止盈/MA20/MACD死叉，生成次日卖出计划。
            for code, pos in list(self.positions.items()):
                row_df = day[day['code'] == code]
                if row_df.empty:
                    continue
                row = row_df.iloc[0]
                self.entry_high[code] = max(self.entry_high.get(code, pos.avg_cost), float(row['high']))
                sell_reason = self._after_close_sell_reason(row, pos)
                if sell_reason:
                    self.pending_sells[code] = sell_reason

            # 盘后15:00后：判断市场强弱，筛出次日候选1-2只。
            if idx + 1 < len(days):
                next_date = days[idx + 1]
                cands = self._screen_after_close(day, date, next_date)
                self.next_day_candidates[next_date] = cands
                self.candidates.extend(cands)

            equity = self._current_equity()
            equity_rows.append({'date': date, 'cash': self.cash, 'market_value': equity - self.cash, 'equity': equity})

        from src.backtest import BacktestResult
        return BacktestResult(self.trades, pd.DataFrame(equity_rows), self.cash, self.positions.copy())

    def _market_regime_by_date(self) -> dict[str, dict[str, float | bool]]:
        out: dict[str, dict[str, float | bool]] = {}
        use_hs300_proxy = bool(self.market_cfg.get('use_hs300_proxy', True))
        weak_below_ma20 = bool(self.market_cfg.get('weak_if_below_ma20', True))
        weak_ma20_down = bool(self.market_cfg.get('weak_if_ma20_down', True))
        weak_pct_lt = float(self.market_cfg.get('weak_if_pct_chg_lt', -0.5))
        require_above_ma = self.market_cfg.get('no_new_position_if_below_ma')
        breadth_threshold = self.market_cfg.get('no_new_position_if_ma20_breadth_lte')
        for date, day in self.data.groupby('date', sort=True):
            ref = day[(day['market'].isin(['SH', 'SZ'])) & (~day['is_st'])]
            breadth_ref = ref
            if ref.empty:
                out[date] = {
                    'weak': False,
                    'pct_chg': 0.0,
                    'close': 0.0,
                    'ma20': 0.0,
                    'ma60': 0.0,
                    'ma20_slope': 0.0,
                    'ma20_breadth': 0.0,
                    'amount': 0.0,
                    'amount_ma20': 0.0,
                    'no_new_position': False,
                }
                continue
            ma20_breadth = 0.0
            if breadth_threshold is not None:
                breadth_valid = breadth_ref[pd.notna(breadth_ref['ma20']) & (breadth_ref['ma20'] > 0)]
                if not breadth_valid.empty:
                    ma20_breadth = float((breadth_valid['close'] > breadth_valid['ma20']).mean())
            # 本地日K没有沪深300指数行，使用全A大额成交权重收盘表现作为沪深300强弱代理。
            if use_hs300_proxy:
                ref = ref.sort_values('amount', ascending=False).head(300)
            weight = ref['amount'].clip(lower=0)
            if float(weight.sum()) <= 0:
                pct_chg = float(ref['pct_chg'].median())
                close = float(ref['close'].median())
                ma20 = float(ref['ma20'].median())
                ma60 = float(ref['ma60'].median())
                prev_ma20 = float(ref['prev_ma20'].median())
            else:
                pct_chg = float((ref['pct_chg'] * weight).sum() / weight.sum())
                close = float((ref['close'] * weight).sum() / weight.sum())
                ma20 = float((ref['ma20'] * weight).sum() / weight.sum())
                ma60 = float((ref['ma60'] * weight).sum() / weight.sum())
                prev_ma20 = float((ref['prev_ma20'] * weight).sum() / weight.sum())
            ma20_slope = ma20 - prev_ma20
            amount = float(ref['amount'].clip(lower=0).sum())
            weak = (pct_chg < weak_pct_lt) or (weak_below_ma20 and close < ma20) or (weak_ma20_down and ma20_slope < 0)
            no_new_position = False
            if require_above_ma:
                ma_col = str(require_above_ma)
                ma_value = ma60 if ma_col == 'ma60' else ma20 if ma_col == 'ma20' else None
                no_new_position = bool(ma_value is not None and ma_value > 0 and close < ma_value)
            if breadth_threshold is not None:
                no_new_position = bool(no_new_position or ma20_breadth <= float(breadth_threshold))
            out[date] = {'weak': bool(weak), 'pct_chg': pct_chg, 'close': close, 'ma20': ma20, 'ma60': ma60, 'ma20_slope': ma20_slope, 'ma20_breadth': ma20_breadth, 'amount': amount, 'no_new_position': no_new_position}
        amount_ratio = self.market_cfg.get('no_new_position_if_amount_below_ma20_ratio')
        if amount_ratio is not None:
            ratio = float(amount_ratio)
            dates = list(out)
            amounts = pd.Series([float(out[d].get('amount', 0.0)) for d in dates], index=dates)
            amount_ma20 = amounts.rolling(20, min_periods=20).mean()
            for d in dates:
                ma = float(amount_ma20.loc[d]) if pd.notna(amount_ma20.loc[d]) else 0.0
                amount = float(out[d].get('amount', 0.0))
                out[d]['amount_ma20'] = ma
                amount_filter = bool(ma > 0 and amount < ma * ratio)
                out[d]['no_new_position'] = bool(out[d].get('no_new_position', False) or amount_filter)
        return out

    def _screen_after_close(self, day: pd.DataFrame, date: str, next_date: str) -> list[Candidate]:
        min_turnover = float(self.buy_cfg.get('min_turnover_pct', 3.0))
        max_turnover = float(self.buy_cfg.get('max_turnover_pct', 20.0))
        min_volume_ratio = float(self.buy_cfg.get('min_volume_ratio', 1.3))
        min_pct = float(self.buy_cfg.get('min_pct_chg', 2.0))
        max_pct = float(self.buy_cfg.get('max_pct_chg', 7.0))
        macd_days = int(self.buy_cfg.get('macd_cross_within_days', 3))
        max_buy_price = float(self.risk_cfg.get('max_buy_price', self.config.initial_cash * self.config.max_position_pct / self.config.lot_size))
        min_avg_amplitude = self.buy_cfg.get('min_avg_amplitude_20d_pct')
        require_recent_ma10_ma20_both_up = bool(self.buy_cfg.get('require_ma10_ma20_both_up_recent20', False))
        require_current_ma_structure = bool(self.buy_cfg.get('require_current_ma10_gt_ma20_both_up', False))
        save_top_n = int(self.buy_cfg.get('save_top_n', 2))
        regime = self.market_by_date.get(date, {})
        if bool(regime.get('no_new_position', False)):
            return []
        if bool(regime.get('weak', False)):
            save_top_n = min(save_top_n, int(self.market_cfg.get('weak_save_top_n', 1)))

        df = day.copy()
        mask = (
            (~df['is_st'].fillna(False))
            & (~df['market'].eq('BJ'))
            & (~df['code'].astype(str).str.startswith(('8', '4')))
            & df['turnover_pct'].between(min_turnover, max_turnover, inclusive='both')
            & (df['volume_ratio'] > min_volume_ratio)
            & df['pct_chg'].between(min_pct, max_pct, inclusive='both')
            & (df['close'] > df['ma20'])
            & (df['ma20'] > df['prev_ma20'])
            & (df['macd_cross_age'].notna())
            & (df['macd_cross_age'] <= macd_days - 1)
            & (df['close'] <= max_buy_price)
            & (~df['is_limit_up'].fillna(False))
            & (~df['paused'].fillna(False))
        )
        if min_avg_amplitude is not None:
            mask = mask & (df['avg_amplitude_20d_pct'] >= float(min_avg_amplitude))
        if require_recent_ma10_ma20_both_up:
            mask = mask & (df['ma10_ma20_both_up_recent20'].fillna(False))
        if require_current_ma_structure:
            mask = mask & (
                df['ma10'].notna()
                & df['ma20'].notna()
                & df['prev_ma10'].notna()
                & df['prev_ma20'].notna()
                & (df['close'] > df['ma10'])
                & (df['ma10'] > df['ma20'])
                & (df['ma10'] > df['prev_ma10'])
                & (df['ma20'] > df['prev_ma20'])
            )
        picked = df[mask].sort_values('pct_chg', ascending=False).head(save_top_n)
        out: list[Candidate] = []
        for rank, row in enumerate(picked.itertuples(index=False), start=1):
            out.append(Candidate(
                signal_date=date,
                buy_date=next_date,
                code=str(row.code),
                rank=rank,
                pct_chg=float(row.pct_chg),
                turnover_pct=float(row.turnover_pct),
                volume_ratio=float(row.volume_ratio),
                close=float(row.close),
                ma20=float(row.ma20),
                reason=f'after_close_top{rank}_market_{"weak" if bool(regime.get("weak", False)) else "strong"}',
            ))
        return out

    def _intraday_recheck(self, row: pd.Series) -> tuple[bool, str]:
        min_pct = float(self.buy_cfg.get('min_pct_chg', 2.0))
        max_pct = float(self.buy_cfg.get('max_pct_chg', 7.0))
        if float(row['close']) < float(row['ma20']):
            return False, 'fallback_below_ma20'
        if float(row['pct_chg']) < min_pct:
            return False, 'fallback_below_pct_floor'
        if float(row['pct_chg']) > max_pct:
            return False, 'pct_overheated'
        if bool(row.get('is_limit_up', False)):
            return False, 'limit_up_not_buyable'
        return True, 'still_above_ma20_pct_floor'

    def _after_close_sell_reason(self, row: pd.Series, pos: Position) -> str | None:
        close = float(row['close'])
        pnl = close / pos.avg_cost - 1 if pos.avg_cost else 0.0
        if pnl <= -float(self.risk_cfg.get('stop_loss_pct', 0.08)):
            return 'fixed_stop_loss_8pct'
        trailing_pct = float(self.risk_cfg.get('trailing_stop_pct', 0.10))
        high_since = self.entry_high.get(str(row['code']), max(pos.avg_cost, float(row['high'])))
        trail_price = high_since * (1 - trailing_pct)
        activation = float(self.risk_cfg.get('trailing_activation_pct', 0.08))
        trailing_ma = self.risk_cfg.get('trailing_require_below_ma')
        trailing_hit = high_since / pos.avg_cost - 1 >= activation and close < trail_price
        if trailing_hit:
            if trailing_ma:
                if pd.notna(row.get(str(trailing_ma))) and close < float(row[str(trailing_ma)]):
                    return f'trailing_take_profit_below_{trailing_ma}'
            else:
                return 'trailing_take_profit'
        sell_ma = str(self.sell_cfg.get('below_ma', 'ma20'))
        if pd.notna(row.get(sell_ma)) and close < float(row[sell_ma]):
            return f'close_below_{sell_ma}'
        if pd.notna(row.get('dif')) and pd.notna(row.get('dea')) and float(row['dif']) < float(row['dea']):
            return 'macd_dead_cross_or_weak'
        return None

    def _buy(self, stamp: str, row: pd.Series, reason: str) -> None:
        if self._open_position_count() >= self.max_positions:
            return
        if bool(row.get('paused', False)) or bool(row.get('is_limit_up', False)):
            return
        date = stamp.split()[0]
        prev_date = self._previous_trading_date(date)
        regime = self.market_by_date.get(prev_date or date, {})
        max_pct = float(self.market_cfg.get('weak_max_position_pct', 0.15)) if bool(regime.get('weak', False)) else self.config.max_position_pct
        price = float(row['close']) * (1 + self.config.slippage_pct)
        max_value = self._current_equity() * max_pct
        budget = min(self.cash, max_value)
        shares = int(budget // (price * self.config.lot_size)) * self.config.lot_size
        if shares <= 0:
            return
        amount = price * shares
        fee = max(amount * self.config.commission_rate, self.config.min_commission)
        if amount + fee > self.cash:
            return
        self.cash -= amount + fee
        code = str(row['code'])
        self.positions[code] = Position(shares=shares, avg_cost=price, buy_date=date)
        self.entry_high[code] = float(row['high'])
        self.trades.append(Trade(stamp, code, 'BUY', price, shares, amount, fee, self.cash, reason))

    def _sell(self, date: str, row: pd.Series, pos: Position, reason: str) -> None:
        if pos.buy_date == date:
            return
        if bool(row.get('paused', False)) or bool(row.get('is_limit_down', False)):
            return
        price = float(row['open']) * (1 - self.config.slippage_pct)
        amount = price * pos.shares
        fee = max(amount * self.config.commission_rate, self.config.min_commission) + amount * self.config.stamp_tax_rate
        pnl = (price - pos.avg_cost) * pos.shares - fee
        pnl_pct = price / pos.avg_cost - 1 if pos.avg_cost else 0.0
        self.cash += amount - fee
        code = str(row['code'])
        self.trades.append(Trade(date + ' 09:30', code, 'SELL', price, pos.shares, amount, fee, self.cash, reason, pnl, pnl_pct))
        self.positions.pop(code, None)
        self.entry_high.pop(code, None)

    def _current_equity(self) -> float:
        return self.cash + sum(pos.shares * self.last_prices.get(code, pos.avg_cost) for code, pos in self.positions.items())

    def _open_position_count(self) -> int:
        return len([p for p in self.positions.values() if p.shares > 0])

    def _previous_trading_date(self, date: str) -> str | None:
        days = sorted(self.data['date'].unique())
        try:
            i = days.index(date)
        except ValueError:
            return None
        return days[i - 1] if i > 0 else None


def prepare_data(spec: StrategySpec, data_root: Path) -> pd.DataFrame:
    loader = LocalDailyData(data_root)
    warmup_start = (pd.to_datetime(spec.start) - pd.Timedelta(days=160)).strftime('%Y-%m-%d')
    codes = spec.codes or None
    data = loader.load_range(warmup_start, spec.end, codes, with_indicators=True)
    if data.empty:
        return data
    data = data.sort_values(['code', 'date']).reset_index(drop=True)
    g = data.groupby('code', group_keys=False)
    data['volume_ratio'] = data['volume'] / g['volume'].transform(lambda s: s.rolling(5, min_periods=5).mean())
    data['amplitude_pct'] = (data['high'] - data['low']) / data['pre_close'] * 100
    data['avg_amplitude_20d_pct'] = g['amplitude_pct'].transform(lambda s: s.rolling(20, min_periods=20).mean())
    data['macd_cross_idx'] = data.groupby('code')['macd_golden_cross'].cumsum()
    ma10_up = data['ma10'].notna() & data['prev_ma10'].notna() & (data['ma10'] > data['prev_ma10'])
    ma20_up = data['ma20'].notna() & data['prev_ma20'].notna() & (data['ma20'] > data['prev_ma20'])
    data['ma10_ma20_both_up'] = ma10_up & ma20_up
    data['ma10_ma20_both_up_recent20'] = g['ma10_ma20_both_up'].transform(lambda s: s.rolling(20, min_periods=1).max()).fillna(False).astype(bool)
    data['row_no'] = data.groupby('code').cumcount()
    cross_rows = data['row_no'].where(data['macd_golden_cross'])
    data['last_cross_row'] = cross_rows.groupby(data['code']).ffill()
    data['macd_cross_age'] = data['row_no'] - data['last_cross_row']
    data = add_cross_sectional_ranks(data)
    return data[(data['date'] >= spec.start) & (data['date'] <= spec.end)].reset_index(drop=True)


def write_candidate_csv(candidates: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(c) for c in candidates]).to_csv(path, index=False, encoding='utf-8-sig')


def main() -> None:
    parser = argparse.ArgumentParser(description='Backtest 4w after-close candidates + next-day 14:40 execution strategy.')
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--data-root', default=str(DEFAULT_DAILY_ROOT))
    parser.add_argument('--report-out', default=None)
    parser.add_argument('--trades-out', default=None)
    parser.add_argument('--equity-out', default=None)
    parser.add_argument('--candidates-out', default=None)
    args = parser.parse_args()

    spec = load_spec(args.strategy)
    data = prepare_data(spec, Path(args.data_root))
    bt = AfterCloseNextDayBacktester(data, spec)
    result = bt.run()

    name = spec.name
    report_out = Path(args.report_out or f'reports/{name}_afterclose_1440_report.md')
    trades_out = Path(args.trades_out or f'generated/{name}_afterclose_1440_trades.csv')
    equity_out = Path(args.equity_out or f'generated/{name}_afterclose_1440_equity.csv')
    candidates_out = Path(args.candidates_out or f'generated/{name}_afterclose_1440_candidates.csv')

    report_path = write_report(spec, result, report_out)
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    equity_out.parent.mkdir(parents=True, exist_ok=True)
    result.trades_frame().to_csv(trades_out, index=False, encoding='utf-8-sig')
    result.equity_curve.to_csv(equity_out, index=False, encoding='utf-8-sig')
    write_candidate_csv(bt.candidates, candidates_out)
    print('summary:', result.summary())
    print('candidate_count:', len(bt.candidates))
    print('report:', report_path)
    print('trades:', trades_out)
    print('equity:', equity_out)
    print('candidates:', candidates_out)


if __name__ == '__main__':
    main()
