# Backtest: INDIA, 2024-04-04 to 2026-10-21

Variant: **no_fresh** - Fix 1: drop 4.3 (uptrend no longer has to be new)

Universe 494 stocks (today's members). Today's watchlist: 30 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 265383 | 3.02 | 0.55 | 49.0 |
| Passes trend template (4.x) | 41050 | 2.42 | -0.13 | 49.1 |
| Trend + entry setup = signal (no fundamentals) | 1211 | 2.67 | 0.23 | 49.8 |
| Signal on today's watchlist (look-ahead) | 113 | 5.59 | 2.28 | 60.2 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 1279, win_rate 34%, avg_win 14.1%, avg_loss -7.4%, payoff 1.90, avg_trade -0.09%
- **Signals on today's watchlist (look-ahead):** trades 120, win_rate 36%, avg_win 19.6%, avg_loss -7.2%, payoff 2.71, avg_trade 2.39%
- Signals skipped by the gap rule (11.6): 37
- Exit reasons: 9.1 stop 724, 9.5 time stop 280, 9.3 weekly close < 50DMA 246, 9.1 breakeven stop 29

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +122.8% vs benchmark +6.7%; max drawdown -27.3%; 45 closed trades, 0 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.