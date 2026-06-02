from __future__ import annotations

import argparse
import os
from pathlib import Path

from src.backtest import BacktestConfig, DailyBacktester
from src.local_data import LocalDailyData, add_indicators
from src.strategies import ma_volume_signal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2026-01-01')
    parser.add_argument('--end', default='2026-04-30')
    parser.add_argument('--codes', default='000001,000002,000006')
    parser.add_argument('--data-root', default=os.getenv('A_SHARE_DAILY_DIR', 'data/daily'))
    args = parser.parse_args()

    codes = [x.strip() for x in args.codes.split(',') if x.strip()]
    data = LocalDailyData(Path(args.data_root)).load_range(args.start, args.end, codes)
    data = add_indicators(data)
    result = DailyBacktester(data, BacktestConfig()).run(ma_volume_signal)

    print('=== ptrade-quant-lab demo summary ===')
    print(result.summary())
    print('=== trades ===')
    for t in result.trades[:50]:
        print(t)
    if len(result.trades) > 50:
        print(f'... {len(result.trades) - 50} more trades')


if __name__ == '__main__':
    main()
