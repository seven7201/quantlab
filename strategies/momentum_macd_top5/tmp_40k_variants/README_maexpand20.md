# Momentum MACD Top5 - 40k After-close MAExpand20

This folder contains the selected 40k after-close / next-day 14:40 execution variant.

## Strategy name

`MA60 + Breadth50 + MAExpand20`

## Core rules

- Initial cash: 40,000
- After-close signal generation, next trading day 14:40 recheck and execution
- Market / HS300 proxy must be above MA60 for new positions
- All-A MA20 breadth must be greater than 50% for new positions
- Candidate must have at least one MA10 and MA20 simultaneous up-slope day in the latest 20 trading days
- Weak market position cap is reduced to 15%
- Maximum positions: 6
- Maximum daily buys: 1
- Fixed stop loss: 8%
- Trailing take-profit activates after +20%, then exits on 8% pullback when below MA10

## Backtest summary

| Period | Final equity | Return | Max drawdown | Buys | Sells | Win rate | PF | Candidates |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2018 | 37,745.61 | -5.64% | -11.61% | 7 | 7 | 28.57% | 0.32 | 81 |
| 2019 | 36,731.33 | -8.17% | -12.03% | 16 | 15 | 40.00% | 0.28 | 74 |
| 2020 | 43,299.16 | +8.25% | -16.81% | 25 | 24 | 41.67% | 1.06 | 187 |
| 2021 | 36,208.84 | -9.48% | -15.94% | 10 | 8 | 0.00% | 0.00 | 93 |
| 2022 | 38,474.57 | -3.81% | -13.08% | 8 | 8 | 25.00% | 0.71 | 103 |
| 2025-2026 | 53,052.79 | +32.63% | -10.13% | 17 | 17 | 41.18% | 2.51 | 163 |

Source local result file: `generated/afterclose_1440_micro_ma60gate_breadth50_maexpand20_compare.csv`.
Generated outputs are gitignored, so the reproducible strategy configs and runner are committed instead.
