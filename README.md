# ptrade-quant-lab

面向国金证券 PTrade 的轻量量化研究系统：

```text
中文策略描述 → YAML 策略规格 → 本地 A股日线回测 → 回测报告 → PTrade 可复制代码
```

## 项目定位

- 不照搬 LEAN 的重型工程，只参考其分层思想。
- 本地回测规则优先贴近 A 股与 PTrade：T+1、100 股整数手、手续费、印花税、涨跌停、持仓比例。
- 最终产物是一份可复制到国金 PTrade 平台的 Python 策略文件。

## 数据源

默认读取本机历史日 K，但文档中不暴露个人机器的绝对路径。可通过环境变量或默认数据目录配置：

```text
A_SHARE_DAILY_DIR=/path/to/local/a-share/daily-k
```

文件组织建议：

```text
${A_SHARE_DAILY_DIR}/YYYY/YYYYMMDD.csv
```

例如：

```text
/path/to/local/a-share/daily-k/2026/20260430.csv
```

已识别字段：

```text
股票代码, 日期, 开盘价, 最高价, 最低价, 收盘价, 昨收价, 涨跌额, 涨跌幅, 成交量, 成交额
```

## 快速运行

```bash
cd ptrade-quant-lab
uv run python tools/run_backtest.py --strategy strategies/ma_volume_breakout/spec.yaml
uv run python tools/generate_ptrade.py --strategy strategies/ma_volume_breakout/spec.yaml
```

如需在本地回测中使用真实历史换手率，可先从东方财富补充并落盘：

```bash
uv run python tools/fetch_turnover.py \
  --start 2026-03-23 \
  --end 2026-04-30 \
  --limit 80 \
  --out data/turnover/eastmoney_turnover.csv

uv run python tools/run_backtest.py \
  --strategy strategies/momentum_macd_top5/spec.yaml \
  --turnover-path data/turnover/eastmoney_turnover.csv
```

`tools/fetch_turnover.py` 支持断点续跑、跳过已存在 `(code,date)`、并发抓取、失败重试和分批落盘。`data/turnover/*.csv` 属于本地可再生数据，默认不提交到 Git。

输出：

```text
reports/ma_volume_breakout_report.md
generated/ma_volume_breakout_ptrade.py
generated/ma_volume_breakout_trades.csv
generated/ma_volume_breakout_equity.csv
```

## 目录结构

```text
src/local_data.py           # 本地历史数据读取与字段标准化
src/indicators.py           # MA/成交量均线/交叉等指标
src/backtest.py             # 日线回测引擎
src/spec.py                 # YAML 策略规格加载
src/strategy_factory.py     # 由 YAML 生成本地回测信号
src/reporting.py            # Markdown 报告
src/ptrade_generator.py     # 生成 PTrade 策略代码
tools/run_backtest.py       # CLI：运行回测
tools/generate_ptrade.py    # CLI：生成 PTrade 文件
strategies/*/spec.yaml      # 策略规格
```

## 当前内置示例

`strategies/ma_volume_breakout/spec.yaml`

策略逻辑：

- MA5 上穿 MA20
- 收盘价在 MA20 上方
- 成交量大于 5 日均量的 1.2 倍
- 非涨停买入
- 跌破 MA10 / 止损 8% / 止盈 15% 卖出
- 单票最多 20% 仓位
