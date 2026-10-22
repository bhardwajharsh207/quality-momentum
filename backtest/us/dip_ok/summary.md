# Backtest: US, 2024-04-18 to 2026-10-21

Variant: **dip_ok** - Fix 1 + Fix 2: drop 4.3, and allow a dip below the 50DMA while above the 200DMA

Universe 503 stocks (today's members). Today's watchlist: 12 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 283985 | 4.59 | 2.75 | 46.1 |
| Passes trend template (4.x) | 60913 | 4.96 | 2.09 | 44.5 |
| Trend + entry setup = signal (no fundamentals) | 1492 | 5.04 | 2.74 | 45.8 |
| Signal on today's watchlist (look-ahead) | 49 | 6.69 | 2.22 | 51.0 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 1585, win_rate 39%, avg_win 15.4%, avg_loss -6.3%, payoff 2.43, avg_trade 2.14%
- **Signals on today's watchlist (look-ahead):** trades 54, win_rate 41%, avg_win 19.5%, avg_loss -7.9%, payoff 2.46, avg_trade 3.23%
- Signals skipped by the gap rule (11.6): 47
- Exit reasons: 9.1 stop 806, 9.5 time stop 442, 9.3 weekly close < 50DMA 317, 9.1 breakeven stop 20

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +31.1% vs benchmark +54.8%; max drawdown -30.2%; 55 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.