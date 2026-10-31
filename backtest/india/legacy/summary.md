# Backtest: INDIA, 2024-04-16 to 2026-10-30

Variant: **legacy** - Rules before v2 (Oct 2026): fresh-cross per config, 4.4-4.7 as gates, no 20DMA pullback

Universe 495 stocks (today's members). Today's watchlist: 35 stocks.

## Does each layer pick better stocks? (60-session forward return from next open)

| layer | observations | avg_60d_return_pct | median_60d_return_pct | pct_beating_benchmark |
|---|---|---|---|---|
| Every stock, every day | 266341 | 2.69 | 0.28 | 48.8 |
| Passes trend template (4.x) | 40993 | 2.07 | -0.38 | 49.0 |
| Trend + entry setup = signal (no fundamentals) | 1211 | 1.93 | -0.4 | 49.1 |
| Signal on today's watchlist (look-ahead) | 126 | 4.49 | 1.47 | 57.1 |

## Every signal as its own trade (exit rules applied, % costs deducted)

- **All signals:** trades 1290, win_rate 34%, avg_win 14.3%, avg_loss -7.4%, payoff 1.92, avg_trade -0.13%
- **Signals on today's watchlist (look-ahead):** trades 135, win_rate 40%, avg_win 16.5%, avg_loss -7.6%, payoff 2.16, avg_trade 2.01%
- Signals skipped by the gap rule (11.6): 36
- Exit reasons: 9.1 stop 734, 9.5 time stop 284, 9.3 weekly close < 50DMA 244, 9.1 breakeven stop 28

## Portfolio: 3 slots, max 2 new/day, equal weight

- Strategy +93.8% vs benchmark +7.2%; max drawdown -25.3%; 47 closed trades, 2 still open.
- No fundamental gate in this simulation, so it understates what the full rules aim for.

## Caveats

- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.
- Universe is today's index members: stocks that were dropped are missing (flatters results).
- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are.