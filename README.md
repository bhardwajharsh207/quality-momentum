# Quality Momentum scanner (India + US)

Implements the "Quality Momentum: Daily Run Spec". Two scripts share one rules engine:

| File | What it does |
|---|---|
| `india_scan.py` | Nifty 500 universe, benchmark Nifty 500 (`^CRSLDX`), prices in ₹ |
| `us_scan.py` | S&P 500 universe, benchmark S&P 500 (`^GSPC`), prices in $ |
| `qm_core.py` | All rules: watchlist (Layer A), signals (Layer B), position checks (Layer C), report |

## Where it runs

Everything runs on GitHub Actions in this repo; nothing is needed on a laptop.

- Both scans try hourly in the evening after their market closes (India 4:47-11:47 PM IST, US about
  2-9 PM Pacific), and again at 7-11 AM local the next morning.
  A Claude scheduled task starts each workflow at :40 past those hours (GitHub's own cron is kept
  only as a backup, since it often runs late or not at all). The first attempt that finds a new session writes the report; later attempts exit in seconds;
  a failed attempt changes nothing, so the next hour retries.
- Reports and state are committed back to `reports/` and `data/`.
- Manual run: Actions tab, pick a workflow, "Run workflow" (args `--force` to redo a session,
  `--rebuild` for a fresh watchlist).
- Edit capital (`capital`) and number of stocks (`n_stocks`) at the bottom of each script.

## How stocks are picked (v2, Oct 2026)

1. **Trend runs on the whole universe every day** (prices only). Each stock gets a stage:
   - **Confirmed uptrend**: close > 50DMA > 200DMA, 50DMA rising (vs 10 days ago), 200DMA rising (vs 20 days ago).
   - **Emerging**: close above a rising 50DMA and above the 200DMA, and the 50DMA either crossed the 200DMA in
     the last 30 sessions, is within 3% below it, or is above it while the 200DMA is still flat.
   - **Pullback in uptrend**: 50DMA > 200DMA, both rising, close dipped to between the two.
   - **Stalling** / **Downtrend**: everything else.
2. **Fundamentals are scored, not gated.** Every rule in sections 2-3 (ROCE/ROE, growth, debt, cash conversion,
   quarterly growth and acceleration) feeds the quality and growth scores. Score = 40% quality + 30% growth +
   30% momentum (6m return, 3m vs index, distance from 52w high), as percentiles across the universe.
3. **Entry gates**: stage must be confirmed or emerging; valuation (section 5, incl. positive EPS and P/E < 100)
   must pass. US also keeps 4.4-4.7 (52w range, relative strength, beat the index) as gates
   (`trend_extra_gates`) and requires the 50/200 golden cross within 65 sessions for entries (`entry_cross_days`);
   India uses 4.4-4.7 only in the score and allows any trend age. Both markets list every stage in the Momentum tab.
4. **Entry setups**: 50-day base breakout on volume, pullback to the 50DMA, or (new) pullback to a rising 20DMA.
   Candidates are ranked by score.

Sheet tabs: **Momentum** lists every stock in a confirmed/emerging uptrend or pullback with its stage, scores
and which fundamental rules it misses. **Watchlist** carries the full write-up for the top 25 by score. In
**Runs**, "Watchlist" now counts stocks in a confirmed/emerging uptrend, and added/removed = stocks that
entered/left that list since the previous session.

`python backtest.py <market> --variant legacy` replays the rules from before v2 for comparison.

## Daily use

Each run processes the previous session's close and writes `reports/<market>/<date>.md`:
BUY list with a rule-by-rule proof table, EXIT list, STOP UPDATES, watchlist changes, and signals that
did not get a slot. A day with nothing to do says "No action today."

- After you buy: add a row to `data/<market>/positions.csv`
  (`ticker,entry_date,entry_price,shares,stop,note`), using your real fill price and the stop from the report.
- After you sell: delete that row.
- The script moves stops to breakeven itself (rule 9.2) and tells you to move your GTT order.

Running twice on the same session does nothing; use `--force` to re-run. `--rebuild` forces a fresh watchlist;
otherwise it rebuilds weekly and refreshes any watchlist stock that has just reported results.

## What the data source cannot check

Data comes from Yahoo Finance through `yfinance`. These gaps are flagged, not hidden:

- **Promoter pledge and holding (2.8, 2.9)** and **ASM/GSM surveillance (1.3)**: not in Yahoo data. Every India
  BUY carries a Screener.in link to check them by hand.
- **History depth**: Yahoo usually has 4 annual years, so "5-year" averages and CAGRs use what is available
  (typically 3-4 years). The proof table shows the year count. Rule 2.10's 10-year test for cyclicals is
  replaced by "ROCE above 15% in each of the last 3 years".
- **Quarterly depth**: rule 3.3 needs 7 quarters. When Yahoo has fewer, the rule shows "unverified"
  and the stock is allowed through. Set `strict_missing=True` in the config to fail it instead.

## Files it keeps

`data/<market>/`: `universe.csv`, `watchlist.csv` (fundamentals: >= 75% of rules pass), `watchlist_full.csv` (every stock with every rule
value), `positions.csv`, `signals.csv`, `missed.csv`, `state.json` (last session processed, cooldowns).
