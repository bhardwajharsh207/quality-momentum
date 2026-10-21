# Backtest: INDIA, 2025-10-13 to 2026-10-19

Universe 495 stocks (today's members). Today's watchlist: 30 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 93198 | 2.41 | -0.24 | 51.8 |
| Passes trend template (4.x) | 3152 | 3.02 | -0.14 | 51.7 |
| Trend + entry setup = signal (no fundamentals) | 116 | 2.66 | 0.58 | 49.1 |
| Signal on today's watchlist (look-ahead) | 7 | -4.39 | -11.4 | 42.9 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 150, win_rate 29%, avg_win 14.3%, avg_loss -7.0%, payoff 2.02, avg_trade -0.94%
- **Signals on today's watchlist (look-ahead):** trades 8, win_rate 38%, avg_win 9.3%, avg_loss -6.5%, payoff 1.44, avg_trade -0.56%
- Signals skipped by the gap rule (11.6): 5
- Exit reasons: 9.1 stop 89, 9.5 time stop 30, 9.3 weekly close < 50DMA 28, 9.1 breakeven stop 3

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +12.8% vs benchmark -5.4%; max drawdown -14.3%; 17 closed trades, 2 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.