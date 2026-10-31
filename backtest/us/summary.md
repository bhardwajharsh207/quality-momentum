# Backtest: US, 2024-04-29 to 2026-10-30

Variant: **current** - v2 rules: confirmed/emerging trend stages, 20DMA pullback entries, gates as configured

Universe 503 stocks (today's members). Today's watchlist: 11 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 284015 | 4.52 | 2.65 | 46.1 |
| Passes trend template (4.x) | 53841 | 4.68 | 1.71 | 44.5 |
| Trend + entry setup = signal (no fundamentals) | 1562 | 4.18 | 1.88 | 44.8 |
| Signal on today's watchlist (look-ahead) | 50 | 6.66 | 3.21 | 52.0 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 1642, win_rate 38%, avg_win 16.1%, avg_loss -6.4%, payoff 2.52, avg_trade 2.15%
- **Signals on today's watchlist (look-ahead):** trades 52, win_rate 50%, avg_win 26.7%, avg_loss -7.4%, payoff 3.60, avg_trade 9.63%
- Signals skipped by the gap rule (11.6): 39
- Exit reasons: 9.1 stop 859, 9.5 time stop 432, 9.3 weekly close < 50DMA 325, 9.1 breakeven stop 26

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +26.3% vs benchmark +48.2%; max drawdown -26.6%; 46 closed trades, 1 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.