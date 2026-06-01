from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.codes import normalize_code

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
EASTMONEY_KLINE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
TURNOVER_COLUMNS = ['code', 'date', 'turnover_pct', 'turnover_source']


@dataclass(frozen=True)
class TurnoverFetchResult:
    code: str
    rows: int
    skipped: bool = False
    error: str | None = None


def eastmoney_secid(code: str | int) -> str:
    normalized = normalize_code(code)
    # 东方财富 secid: 沪市=1, 深市/北交所=0。北交所本策略默认排除，不单独补。
    market = '1' if normalized.startswith(('6', '9')) else '0'
    return f'{market}.{normalized}'


def safe_float(value) -> float:
    try:
        if value in ('', None, '-'):
            return 0.0
        if isinstance(value, str):
            value = value.replace(',', '').replace('%', '').strip()
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_url(url: str, timeout: int = 12, retries: int = 2) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = Request(url, headers={'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8')
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(last_exc or 'request failed')


def fetch_eastmoney_turnover(
    code: str | int,
    limit: int = 180,
    start: str | None = None,
    end: str | None = None,
    timeout: int = 12,
    retries: int = 2,
) -> pd.DataFrame:
    """Fetch daily turnover rate (%) from Eastmoney kline API for one A-share code."""
    normalized = normalize_code(code)
    params = {
        'secid': eastmoney_secid(normalized),
        'fields1': 'f1,f2,f3,f4,f5,f6',
        # f61 = 换手率。原始 kline 每行: 日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手率
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101',
        'fqt': '1',
        'end': (end or '2050-01-01').replace('-', ''),
        'lmt': str(limit),
    }
    if start:
        params['beg'] = start.replace('-', '')
    url = EASTMONEY_KLINE_URL + '?' + urlencode(params)
    payload = json.loads(read_url(url, timeout=timeout, retries=retries))
    raw_rows = ((payload.get('data') or {}).get('klines') or [])
    rows: list[dict] = []
    for raw in raw_rows:
        parts = raw.split(',')
        if len(parts) < 11:
            continue
        rows.append(
            {
                'code': normalized,
                'date': parts[0],
                'turnover_pct': safe_float(parts[10]),
                'turnover_source': 'eastmoney',
            }
        )
    out = pd.DataFrame(rows, columns=TURNOVER_COLUMNS)
    if out.empty:
        return out
    return normalize_turnover_frame(out)


def fetch_turnover_for_codes(codes: Iterable[str | int], limit: int = 180, sleep_sec: float = 0.05) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for raw_code in sorted({normalize_code(c) for c in codes}):
        try:
            df = fetch_eastmoney_turnover(raw_code, limit=limit)
        except Exception as exc:  # noqa: BLE001
            print(f'warn: fetch turnover failed for {raw_code}: {exc}')
            continue
        if not df.empty:
            frames.append(df)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    if not frames:
        return pd.DataFrame(columns=TURNOVER_COLUMNS)
    return normalize_turnover_frame(pd.concat(frames, ignore_index=True))


def normalize_turnover_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=TURNOVER_COLUMNS)
    out = df.copy()
    out['code'] = out['code'].map(normalize_code)
    out['date'] = pd.to_datetime(out['date']).dt.strftime('%Y-%m-%d')
    out['turnover_pct'] = pd.to_numeric(out['turnover_pct'], errors='coerce')
    if 'turnover_source' not in out.columns:
        out['turnover_source'] = 'eastmoney'
    out = out.dropna(subset=['turnover_pct']).drop_duplicates(['code', 'date'], keep='last')
    return out[TURNOVER_COLUMNS].sort_values(['date', 'code']).reset_index(drop=True)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', delete=False, dir=path.parent, suffix='.tmp', encoding='utf-8-sig', newline='') as tmp:
        tmp_path = Path(tmp.name)
        df.to_csv(tmp, index=False)
    tmp_path.replace(path)
    return path


def write_turnover_cache(df: pd.DataFrame, path: Path) -> Path:
    return atomic_write_csv(normalize_turnover_frame(df), path)


def load_turnover_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=TURNOVER_COLUMNS)
    df = pd.read_csv(path, encoding='utf-8-sig')
    required = {'code', 'date', 'turnover_pct'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f'换手率落盘文件字段缺失: {sorted(missing)}')
    return normalize_turnover_frame(df)


def merge_turnover_frames(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    frames = [df for df in [existing, incoming] if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame(columns=TURNOVER_COLUMNS)
    return normalize_turnover_frame(pd.concat(frames, ignore_index=True))


def target_pairs_from_daily(daily: pd.DataFrame, start: str | None = None, end: str | None = None) -> set[tuple[str, str]]:
    if daily.empty:
        return set()
    frame = daily[['code', 'date']].copy()
    frame['code'] = frame['code'].map(normalize_code)
    frame['date'] = pd.to_datetime(frame['date']).dt.strftime('%Y-%m-%d')
    if start:
        frame = frame[frame['date'] >= start]
    if end:
        frame = frame[frame['date'] <= end]
    return set(frame.itertuples(index=False, name=None))


def missing_codes_by_date(
    target_pairs: set[tuple[str, str]],
    existing: pd.DataFrame,
) -> dict[str, set[str]]:
    existing_pairs = set()
    if not existing.empty:
        existing_pairs = set(existing[['code', 'date']].itertuples(index=False, name=None))
    missing: dict[str, set[str]] = {}
    for code, date in target_pairs - existing_pairs:
        missing.setdefault(code, set()).add(date)
    return missing


def fetch_missing_turnover_to_disk(
    target_pairs: set[tuple[str, str]],
    out_path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 180,
    workers: int = 8,
    batch_size: int = 100,
    timeout: int = 12,
    retries: int = 2,
    sleep_sec: float = 0.0,
    failures_path: Path | None = None,
    verbose: bool = True,
) -> tuple[pd.DataFrame, list[TurnoverFetchResult]]:
    """Fetch only missing (code, date) turnover rows and persist merged real data to disk.

    This is a durable local dataset writer, not an in-memory cache: it loads existing
    CSV rows, skips already-covered pairs, fetches missing dates by code concurrently,
    and atomically rewrites the merged CSV after each batch for resumable runs.
    """
    existing = load_turnover_cache(out_path)
    missing_by_code = missing_codes_by_date(target_pairs, existing)
    codes = sorted(missing_by_code)
    results: list[TurnoverFetchResult] = []
    failures: list[dict[str, str]] = []
    if not codes:
        if verbose:
            print(f'no missing turnover rows; existing rows={len(existing)} path={out_path}')
        return existing, results

    if verbose:
        missing_rows = sum(len(v) for v in missing_by_code.values())
        print(f'missing turnover rows={missing_rows}, codes={len(codes)}, existing rows={len(existing)}')

    def fetch_one(code: str) -> tuple[TurnoverFetchResult, pd.DataFrame]:
        if sleep_sec > 0:
            time.sleep(sleep_sec)
        try:
            df = fetch_eastmoney_turnover(code, limit=limit, start=start, end=end, timeout=timeout, retries=retries)
            wanted_dates = missing_by_code[code]
            if not df.empty:
                df = df[df['date'].isin(wanted_dates)]
            return TurnoverFetchResult(code=code, rows=len(df)), df
        except Exception as exc:  # noqa: BLE001
            return TurnoverFetchResult(code=code, rows=0, error=str(exc)), pd.DataFrame(columns=TURNOVER_COLUMNS)

    for batch_start in range(0, len(codes), batch_size):
        batch_codes = codes[batch_start : batch_start + batch_size]
        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(fetch_one, code): code for code in batch_codes}
            for future in as_completed(futures):
                result, df = future.result()
                results.append(result)
                if result.error:
                    failures.append({'code': result.code, 'error': result.error})
                    if verbose:
                        print(f'warn: fetch turnover failed for {result.code}: {result.error}')
                elif not df.empty:
                    frames.append(df)
        if frames:
            existing = merge_turnover_frames(existing, pd.concat(frames, ignore_index=True))
            atomic_write_csv(existing, out_path)
        elif not out_path.exists():
            atomic_write_csv(existing, out_path)
        if failures_path is not None:
            write_failures(failures, failures_path)
        if verbose:
            done_codes = min(batch_start + len(batch_codes), len(codes))
            got_rows = sum(r.rows for r in results)
            print(f'progress: {done_codes}/{len(codes)} codes, fetched rows={got_rows}, total rows={len(existing)}')

    return existing, results


def write_failures(failures: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not failures:
        if path.exists():
            path.unlink()
        return
    pd.DataFrame(failures).drop_duplicates(['code'], keep='last').sort_values('code').to_csv(
        path, index=False, encoding='utf-8-sig'
    )
