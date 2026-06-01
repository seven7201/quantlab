from __future__ import annotations

import argparse
from pathlib import Path

from src.backtest import BacktestConfig, DailyBacktester
from src.local_data import LocalDailyData
from src.reporting import write_report
from src.spec import load_spec
from src.strategy_factory import build_signal


def main() -> None:
    parser = argparse.ArgumentParser(description='Run local A-share daily backtest from a strategy YAML spec.')
    parser.add_argument('--strategy', required=True, help='Path to strategy spec.yaml')
    parser.add_argument('--data-root', default='/Users/mac/股票/炒股/日k')
    parser.add_argument('--report-out', default=None)
    parser.add_argument('--trades-out', default=None)
    parser.add_argument('--equity-out', default=None)
    parser.add_argument('--turnover-path', default=None, help='Optional durable CSV dataset with real turnover columns: code,date,turnover_pct')
    args = parser.parse_args()

    spec = load_spec(args.strategy)
    data = LocalDailyData(Path(args.data_root)).load_range(
        spec.start,
        spec.end,
        spec.codes,
        with_indicators=True,
        turnover_path=args.turnover_path,
    )
    config = BacktestConfig(
        initial_cash=spec.initial_cash,
        max_position_pct=spec.max_position_pct,
        max_daily_buys=spec.max_daily_buys,
        buy_rank_field=spec.buy_rank_field,
        buy_rank_ascending=spec.buy_rank_ascending,
    )
    result = DailyBacktester(data, config).run(build_signal(spec))

    name = spec.name
    report_out = Path(args.report_out or f'reports/{name}_report.md')
    trades_out = Path(args.trades_out or f'generated/{name}_trades.csv')
    equity_out = Path(args.equity_out or f'generated/{name}_equity.csv')
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
