from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class StrategySpec:
    raw: dict[str, Any]
    path: Path

    @property
    def name(self) -> str:
        return str(self.raw.get('name', self.path.parent.name))

    @property
    def start(self) -> str:
        return str(self.raw['backtest']['start'])

    @property
    def end(self) -> str:
        return str(self.raw['backtest']['end'])

    @property
    def codes(self) -> list[str]:
        return [str(x) for x in self.raw.get('universe', {}).get('codes', [])]

    @property
    def initial_cash(self) -> float:
        return float(self.raw.get('backtest', {}).get('initial_cash', 1_000_000))

    @property
    def max_position_pct(self) -> float:
        return float(self.raw.get('risk', {}).get('max_position_pct', 0.2))

    @property
    def max_daily_buys(self) -> int | None:
        value = self.raw.get('risk', {}).get('max_daily_buys', self.raw.get('buy', {}).get('max_daily_buys'))
        return int(value) if value is not None else None

    @property
    def buy_rank_field(self) -> str | None:
        value = self.raw.get('buy', {}).get('rank_by')
        return str(value) if value else None

    @property
    def buy_rank_ascending(self) -> bool:
        return bool(self.raw.get('buy', {}).get('rank_ascending', False))

    @property
    def stop_loss_pct(self) -> float:
        return float(self.raw.get('risk', {}).get('stop_loss_pct', 0.08))

    @property
    def take_profit_pct(self) -> float:
        return float(self.raw.get('risk', {}).get('take_profit_pct', 0.15))


def load_spec(path: str | Path) -> StrategySpec:
    p = Path(path)
    with p.open('r', encoding='utf-8') as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f'策略规格不是 YAML object: {p}')
    return StrategySpec(raw=raw, path=p)
