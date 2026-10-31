# Backtest: INDIA, 2024-04-16 to 2026-10-30

Variant: **current** - v2 rules: confirmed/emerging trend stages, 20DMA pullback entries, gates as configured

Universe 495 stocks (today's members). Today's watchlist: 35 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 266341 | 2.69 | 0.28 | 48.8 |
| Passes trend template (4.x) | 79920 | 2.1 | -0.11 | 48.8 |
| Trend + entry setup = signal (no fundamentals) | 2221 | 2.34 | -0.02 | 48.4 |
| Signal on today's watchlist (look-ahead) | 188 | 7.57 | 6.22 | 59.0 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 2357, win_rate 35%, avg_win 14.5%, avg_loss -6.6%, payoff 2.21, avg_trade 0.75%
- **Signals on today's watchlist (look-ahead):** trades 200, win_rate 44%, avg_win 15.7%, avg_loss -6.8%, payoff 2.30, avg_trade 3.09%
- Signals skipped by the gap rule (11.6): 31
- Exit reasons: 9.1 stop 1357, 9.5 time stop 482, 9.3 weekly close < 50DMA 475, 9.1 breakeven stop 43

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +23.9% vs benchmark +7.2%; max drawdown -41.9%; 58 closed trades, 1 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.