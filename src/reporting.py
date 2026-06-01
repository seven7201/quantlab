from __future__ import annotations

from pathlib import Path

from src.backtest import BacktestResult
from src.spec import StrategySpec


def write_report(spec: StrategySpec, result: BacktestResult, out: str | Path) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = result.summary()
    trades = result.trades_frame()

    lines = [
        f'# {spec.name} 回测报告',
        '',
        '## 回测设置',
        '',
        f'- 区间：{spec.start} ~ {spec.end}',
        f'- 股票池：{_format_universe(spec)}',
        f'- 初始资金：{summary["initial_cash"]:,.2f}',
        f'- 单票最大仓位：{spec.max_position_pct:.0%}',
        '',
        '## 核心结果',
        '',
        f'- 期末权益：{summary["final_equity"]:,.2f}',
        f'- 总收益：{summary["total_return"]:.2%}',
        f'- 年化收益：{summary["annual_return"]:.2%}',
        f'- 最大回撤：{summary["max_drawdown"]:.2%}',
        f'- 交易次数：{summary["trade_count"]}',
        f'- 买入次数：{summary["buy_count"]}',
        f'- 卖出次数：{summary["sell_count"]}',
        f'- 胜率：{summary["win_rate"]:.2%}',
        f'- 盈亏比/Profit Factor：{summary["profit_factor"]}',
        f'- 期末持仓数：{summary["open_positions"]}',
        '',
        '## 最近交易明细',
        '',
    ]
    if trades.empty:
        lines.append('无交易。')
    else:
        show = trades.tail(20).copy()
        lines.append(show.to_markdown(index=False))
    lines.extend([
        '',
        '## 风险提示',
        '',
        '- 当前本地日线级 MVP 回测，PTrade 平台执行前仍需用平台回测校验 API 兼容性。',
        '- 换手率：若通过 --turnover-path 接入 data/turnover/eastmoney_turnover.csv，则使用本地真实落盘的东方财富历史换手率；否则退回成交额/1亿元流动性代理。',
        '- ST 过滤依赖本地数据是否包含股票名称/ST标记；当前本地日K未含名称时无法识别历史ST，仅保留代码/市场规则过滤。',
    ])
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def _format_universe(spec: StrategySpec) -> str:
    return ', '.join(spec.codes) if spec.codes else '本地日K全市场'
