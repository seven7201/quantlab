from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.codes import normalize_code
from src.local_data import LocalDailyData
from src.turnover import (
    fetch_missing_turnover_to_disk,
    fetch_turnover_for_codes,
    target_pairs_from_daily,
    write_turnover_cache,
)


def discover_local_daily(data_root: str, start: str | None, end: str | None, codes: list[str] | None) -> pd.DataFrame:
    loader = LocalDailyData(Path(data_root))
    wanted = {normalize_code(c) for c in codes} if codes else None
    frames: list[pd.DataFrame] = []
    for path in loader.available_files(start, end):
        day = loader.read_day(f'{path.stem[:4]}-{path.stem[4:6]}-{path.stem[6:]}')
        if wanted:
            day = day[day['code'].isin(wanted)]
        if not day.empty:
            frames.append(day[['code', 'date']])
    if not frames:
        return pd.DataFrame(columns=['code', 'date'])
    return pd.concat(frames, ignore_index=True).drop_duplicates(['code', 'date'])


def main() -> None:
    parser = argparse.ArgumentParser(description='Fetch Eastmoney daily turnover rate (%) and persist local real turnover dataset.')
    parser.add_argument('--data-root', default='/Users/mac/股票/炒股/日k')
    parser.add_argument('--start', default=None, help='Optional start date yyyy-mm-dd')
    parser.add_argument('--end', default=None, help='Optional end date yyyy-mm-dd')
    parser.add_argument('--codes', nargs='*', default=None, help='Optional explicit stock codes')
    parser.add_argument('--limit', type=int, default=180, help='Kline rows per code to fetch')
    parser.add_argument('--out', default='data/turnover/eastmoney_turnover.csv', help='Durable local CSV dataset path')
    parser.add_argument('--workers', type=int, default=8, help='Concurrent HTTP workers')
    parser.add_argument('--batch-size', type=int, default=100, help='Persist after each N codes')
    parser.add_argument('--timeout', type=int, default=12)
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--sleep-sec', type=float, default=0.0, help='Optional per-task pre-request delay')
    parser.add_argument('--failures-out', default='data/turnover/eastmoney_turnover_failures.csv')
    parser.add_argument(
        '--legacy-overwrite',
        action='store_true',
        help='Old behavior: fetch requested codes and overwrite --out; not resumable. Prefer default durable mode.',
    )
    args = parser.parse_args()

    out_path = Path(args.out)

    if args.legacy_overwrite:
        if args.codes:
            codes = args.codes
        else:
            daily = discover_local_daily(args.data_root, args.start, args.end, None)
            codes = sorted(daily['code'].unique().tolist())
        print(f'legacy fetching turnover for {len(codes)} codes ...')
        df = fetch_turnover_for_codes(codes, limit=args.limit, sleep_sec=args.sleep_sec)
        path = write_turnover_cache(df, out_path)
        print(f'wrote {len(df)} rows to {path}')
        return

    daily = discover_local_daily(args.data_root, args.start, args.end, args.codes)
    target_pairs = target_pairs_from_daily(daily, args.start, args.end)
    if not target_pairs:
        print('no local daily rows matched; nothing to fetch')
        return

    df, results = fetch_missing_turnover_to_disk(
        target_pairs,
        out_path,
        start=args.start,
        end=args.end,
        limit=args.limit,
        workers=args.workers,
        batch_size=args.batch_size,
        timeout=args.timeout,
        retries=args.retries,
        sleep_sec=args.sleep_sec,
        failures_path=Path(args.failures_out) if args.failures_out else None,
    )
    failed = sum(1 for r in results if r.error)
    fetched = sum(r.rows for r in results)
    print(f'done: fetched_rows={fetched}, failed_codes={failed}, total_rows={len(df)}, path={out_path}')


if __name__ == '__main__':
    main()
