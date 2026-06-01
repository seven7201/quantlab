from __future__ import annotations


def normalize_code(code: str | int) -> str:
    s = str(code).strip().lower()
    for prefix in ('shse.', 'szse.', 'sse.', 'sh.', 'sz.', 'bj.', 'sh', 'sz', 'bj'):
        s = s.replace(prefix, '')
    return s.zfill(6)[-6:]


def infer_market(code: str | int) -> str:
    c = normalize_code(code)
    if c.startswith(('8', '4')):
        return 'BJ'
    if c.startswith(('6', '9')):
        return 'SH'
    return 'SZ'


def to_ptrade_code(code: str | int) -> str:
    c = normalize_code(code)
    if c.startswith(('8', '4')):
        return f'{c}.BJ'
    if c.startswith(('6', '9')):
        return f'{c}.SS'
    return f'{c}.SZ'
