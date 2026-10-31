# Backtest: US, 2024-04-29 to 2026-10-30

Variant: **legacy** - Rules before v2 (Oct 2026): fresh-cross per config, 4.4-4.7 as gates, no 20DMA pullback

Universe 503 stocks (today's members). Today's watchlist: 11 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 284015 | 4.52 | 2.65 | 46.1 |
| Passes trend template (4.x) | 10076 | 7.55 | 3.58 | 47.5 |
| Trend + entry setup = signal (no fundamentals) | 308 | 5.01 | 2.32 | 41.9 |
| Signal on today's watchlist (look-ahead) | 8 | 13.55 | 12.38 | 62.5 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 328, win_rate 42%, avg_win 24.0%, avg_loss -6.5%, payoff 3.69, avg_trade 6.33%
- **Signals on today's watchlist (look-ahead):** trades 8, win_rate 50%, avg_win 52.8%, avg_loss -7.2%, payoff 7.30, avg_trade 22.80%
- Signals skipped by the gap rule (11.6): 18
- Exit reasons: 9.1 stop 157, 9.5 time stop 97, 9.3 weekly close < 50DMA 68, 9.1 breakeven stop 6

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +55.2% vs benchmark +48.2%; max drawdown -22.9%; 42 closed trades, 1 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.