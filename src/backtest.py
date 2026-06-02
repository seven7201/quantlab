from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

import pandas as pd


@dataclass
class Position:
    shares: int = 0
    avg_cost: float = 0.0
    buy_date: str | None = None


@dataclass
class Trade:
    date: str
    code: str
    side: str
    price: float
    shares: int
    amount: float
    fee: float
    cash_after: float
    reason: str
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    max_position_pct: float = 0.2
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_pct: float = 0.0005
    lot_size: int = 100
    limit_pct: float = 9.8
    price_field: str = 'close'
    max_daily_buys: int | None = None
    buy_rank_field: str | None = None
    buy_rank_ascending: bool = False
    weak_market_max_position_pct: float | None = None
    weak_market_max_daily_buys: int | None = None


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.DataFrame
    final_cash: float
    final_positions: dict[str, Position]

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])

    def summary(self) -> dict[str, float | int]:
        if self.equity_curve.empty:
            return {'total_return': 0.0, 'annual_return': 0.0, 'max_drawdown': 0.0, 'trade_count': 0, 'win_rate': 0.0}
        curve = self.equity_curve.copy()
        start = float(curve.iloc[0]['equity'])
        end = float(curve.iloc[-1]['equity'])
        peak = curve['equity'].cummax()
        drawdown = curve['equity'] / peak - 1
        sell_trades = [t for t in self.trades if t.side == 'SELL']
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl <= 0]
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        days = max(len(curve), 1)
        total_return = end / start - 1 if start else 0.0
        annual_return = (1 + total_return) ** (252 / days) - 1 if total_return > -1 else -1.0
        return {
            'initial_cash': start,
            'final_equity': end,
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': float(drawdown.min()),
            'trade_count': len(self.trades),
            'buy_count': len([t for t in self.trades if t.side == 'BUY']),
            'sell_count': len(sell_trades),
            'win_rate': len(wins) / len(sell_trades) if sell_trades else 0.0,
            'profit_factor': gross_profit / gross_loss if gross_loss else (float('inf') if gross_profit else 0.0),
            'open_positions': len([p for p in self.final_positions.values() if p.shares > 0]),
        }


SignalFunc = Callable[[pd.Series, Position | None], tuple[str | None, str]]


class DailyBacktester:
    def __init__(self, data: pd.DataFrame, config: BacktestConfig | None = None):
        self.data = data.sort_values(['date', 'code']).reset_index(drop=True)
        self.config = config or BacktestConfig()
        self.cash = self.config.initial_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.last_prices: dict[str, float] = {}

    def run(self, signal_func: SignalFunc) -> BacktestResult:
        equity_rows = []
        for date, day in self.data.groupby('date', sort=True):
            for _, row in day.iterrows():
                self.last_prices[str(row['code'])] = float(row[self.config.price_field])

            # 先卖后买，避免当日信号资金占用不释放。
            for _, row in day.iterrows():
                code = str(row['code'])
                pos = self.positions.get(code)
                signal, reason = signal_func(row, pos)
                if signal == 'SELL' and pos and pos.shares > 0:
                    self._sell(date, row, pos, reason)

            buy_candidates: list[tuple[pd.Series, str]] = []
            for _, row in day.iterrows():
                code = str(row['code'])
                pos = self.positions.get(code)
                signal, reason = signal_func(row, pos)
                if signal == 'BUY' and (not pos or pos.shares <= 0):
                    buy_candidates.append((row, reason))

            buy_candidates = self._rank_buy_candidates(buy_candidates)
            if self.config.max_daily_buys is not None:
                buy_candidates = buy_candidates[: self.config.max_daily_buys]
            for row, reason in buy_candidates:
                self._buy(date, row, reason)

            equity = self._current_equity()
            equity_rows.append({'date': date, 'cash': self.cash, 'market_value': equity - self.cash, 'equity': equity})

        return BacktestResult(self.trades, pd.DataFrame(equity_rows), self.cash, self.positions.copy())

    def _rank_buy_candidates(self, candidates: list[tuple[pd.Series, str]]) -> list[tuple[pd.Series, str]]:
        field = self.config.buy_rank_field
        if not field:
            return candidates
        return sorted(
            candidates,
            key=lambda item: (
                pd.isna(item[0].get(field)),
                float(item[0].get(field, float('-inf')) if pd.notna(item[0].get(field)) else float('-inf')),
            ),
            reverse=not self.config.buy_rank_ascending,
        )

    def _buy(self, date: str, row: pd.Series, reason: str) -> None:
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
        self.positions[str(row['code'])] = Position(shares=shares, avg_cost=price, buy_date=date)
        self.trades.append(Trade(date, str(row['code']), 'BUY', price, shares, amount, fee, self.cash, reason))

    def _sell(self, date: str, row: pd.Series, pos: Position, reason: str) -> None:
        if pos.buy_date == date:  # A股 T+1
            return
        if bool(row.get('paused', False)) or self._is_limit_down(row):
            return
        price = float(row[self.config.price_field]) * (1 - self.config.slippage_pct)
        amount = price * pos.shares
        fee = max(amount * self.config.commission_rate, self.config.min_commission) + amount * self.config.stamp_tax_rate
        pnl = (price - pos.avg_cost) * pos.shares - fee
        pnl_pct = price / pos.avg_cost - 1 if pos.avg_cost else 0.0
        self.cash += amount - fee
        self.trades.append(Trade(date, str(row['code']), 'SELL', price, pos.shares, amount, fee, self.cash, reason, pnl, pnl_pct))
        self.positions.pop(str(row['code']), None)

    def _current_equity(self) -> float:
        return self.cash + sum(pos.shares * self.last_prices.get(code, pos.avg_cost) for code, pos in self.positions.items())

    def _is_limit_up(self, row: pd.Series) -> bool:
        return float(row.get('pct_chg', 0) or 0) >= self.config.limit_pct

    def _is_limit_down(self, row: pd.Series) -> bool:
        return float(row.get('pct_chg', 0) or 0) <= -self.config.limit_pct
