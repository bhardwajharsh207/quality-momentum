# Backtest: INDIA, 2024-04-04 to 2026-10-21

Variant: **dip_ok** - Fix 1 + Fix 2: drop 4.3, and allow a dip below the 50DMA while above the 200DMA

Universe 495 stocks (today's members). Today's watchlist: 30 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 265952 | 3.02 | 0.57 | 49.0 |
| Passes trend template (4.x) | 49498 | 2.26 | -0.37 | 48.2 |
| Trend + entry setup = signal (no fundamentals) | 1353 | 2.31 | -0.07 | 48.5 |
| Signal on today's watchlist (look-ahead) | 123 | 5.24 | 0.36 | 58.5 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 1429, win_rate 33%, avg_win 13.7%, avg_loss -7.1%, payoff 1.92, avg_trade -0.25%
- **Signals on today's watchlist (look-ahead):** trades 128, win_rate 37%, avg_win 18.2%, avg_loss -7.0%, payoff 2.61, avg_trade 2.27%
- Signals skipped by the gap rule (11.6): 39
- Exit reasons: 9.1 stop 839, 9.5 time stop 294, 9.3 weekly close < 50DMA 264, 9.1 breakeven stop 32

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +56.7% vs benchmark +6.7%; max drawdown -28.2%; 57 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.