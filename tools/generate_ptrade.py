from __future__ import annotations

import argparse
from pathlib import Path

from src.ptrade_generator import write_ptrade_code
from src.spec import load_spec


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate PTrade strategy code from YAML spec.')
    parser.add_argument('--strategy', default='strategies/ma_volume_breakout/spec.yaml')
    parser.add_argument('--out', default=None)
    args = parser.parse_args()
    spec = load_spec(args.strategy)
    out = Path(args.out or f'generated/{spec.name}_ptrade.py')
    path = write_ptrade_code(spec, out)
    print(f'generated: {path}')


if __name__ == '__main__':
    main()
