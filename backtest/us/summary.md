# Backtest: US, 2024-04-29 to 2026-10-30

Variant: **current** - v2 rules: confirmed/emerging trend stages, 20DMA pullback entries, gates as configured

Universe 503 stocks (today's members). Today's watchlist: 11 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 284015 | 4.52 | 2.65 | 46.1 |
| Passes trend template (4.x) | 11488 | 7.19 | 3.41 | 47.5 |
| Trend + entry setup = signal (no fundamentals) | 431 | 5.23 | 2.81 | 43.9 |
| Signal on today's watchlist (look-ahead) | 11 | 18.62 | 10.3 | 54.5 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 459, win_rate 41%, avg_win 26.4%, avg_loss -6.7%, payoff 3.93, avg_trade 6.69%
- **Signals on today's watchlist (look-ahead):** trades 12, win_rate 67%, avg_win 50.7%, avg_loss -5.8%, payoff 8.68, avg_trade 31.83%
- Signals skipped by the gap rule (11.6): 15
- Exit reasons: 9.1 stop 239, 9.3 weekly close < 50DMA 108, 9.5 time stop 106, 9.1 breakeven stop 6

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +195.3% vs benchmark +48.2%; max drawdown -21.9%; 43 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.