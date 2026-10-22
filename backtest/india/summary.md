# Backtest: INDIA, 2024-04-04 to 2026-10-21

Variant: **current** - Rules as live today

Universe 495 stocks (today's members). Today's watchlist: 30 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 265952 | 3.02 | 0.57 | 49.0 |
| Passes trend template (4.x) | 9190 | 3.23 | 0.31 | 47.9 |
| Trend + entry setup = signal (no fundamentals) | 318 | 3.52 | 0.96 | 48.7 |
| Signal on today's watchlist (look-ahead) | 19 | 0.32 | -0.48 | 52.6 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 354, win_rate 37%, avg_win 14.4%, avg_loss -6.9%, payoff 2.09, avg_trade 1.04%
- **Signals on today's watchlist (look-ahead):** trades 20, win_rate 40%, avg_win 11.9%, avg_loss -5.1%, payoff 2.31, avg_trade 1.66%
- Signals skipped by the gap rule (11.6): 12
- Exit reasons: 9.1 stop 183, 9.5 time stop 94, 9.3 weekly close < 50DMA 72, 9.1 breakeven stop 5

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +103.0% vs benchmark +6.7%; max drawdown -30.9%; 49 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.