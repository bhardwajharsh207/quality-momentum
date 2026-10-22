# Backtest: US, 2024-04-18 to 2026-10-21

Variant: **current** - Rules as live today

Universe 503 stocks (today's members). Today's watchlist: 12 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 283985 | 4.59 | 2.75 | 46.1 |
| Passes trend template (4.x) | 9980 | 7.61 | 3.62 | 47.3 |
| Trend + entry setup = signal (no fundamentals) | 299 | 5.2 | 2.81 | 41.8 |
| Signal on today's watchlist (look-ahead) | 8 | 13.55 | 12.38 | 62.5 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 328, win_rate 42%, avg_win 24.0%, avg_loss -6.5%, payoff 3.71, avg_trade 6.35%
- **Signals on today's watchlist (look-ahead):** trades 8, win_rate 50%, avg_win 52.8%, avg_loss -7.2%, payoff 7.30, avg_trade 22.80%
- Signals skipped by the gap rule (11.6): 19
- Exit reasons: 9.1 stop 156, 9.5 time stop 98, 9.3 weekly close < 50DMA 68, 9.1 breakeven stop 6

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +59.5% vs benchmark +54.8%; max drawdown -22.9%; 41 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.