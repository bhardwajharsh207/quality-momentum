# Backtest: US, 2025-10-21 to 2026-10-21

Universe 503 stocks (today's members). Today's watchlist: 12 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 95569 | 5.02 | 2.54 | 46.7 |
| Passes trend template (4.x) | 3178 | 5.32 | 0.99 | 42.3 |
| Trend + entry setup = signal (no fundamentals) | 97 | 4.68 | 0.15 | 42.3 |
| Signal on today's watchlist (look-ahead) | 0 | nan | nan | nan |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 126, win_rate 33%, avg_win 12.3%, avg_loss -6.7%, payoff 1.85, avg_trade -0.34%
- **Signals on today's watchlist (look-ahead):** none
- Signals skipped by the gap rule (11.6): 11
- Exit reasons: 9.1 stop 67, 9.5 time stop 34, 9.3 weekly close < 50DMA 20, 9.1 breakeven stop 5

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +28.9% vs benchmark +15.2%; max drawdown -25.9%; 20 closed trades, 1 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.