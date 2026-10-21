# Quality Momentum scanner (India + US)

Implements the "Quality Momentum: Daily Run Spec". Two scripts share one rules engine:

| File | What it does |
|---|---|
| `india_scan.py` | Nifty 500 universe, benchmark Nifty 500 (`^CRSLDX`), prices in ₹ |
| `us_scan.py` | S&P 500 universe, benchmark S&P 500 (`^GSPC`), prices in $ |
| `qm_core.py` | All rules: watchlist (Layer A), signals (Layer B), position checks (Layer C), report |

## Setup (once)

```bash
cd quality-momentum
python3 -m pip install -r requirements.txt
python3 india_scan.py --rebuild     # first run builds the watchlist: ~15-30 min
python3 us_scan.py --rebuild
```

Edit capital (`capital`) and number of stocks (`n_stocks`) at the bottom of each script, or pass
`--capital 25000 --n 3` on the command line.

## Schedule (cron, Mac or Linux)

Run `crontab -e` and add:

```
0  * * * * cd ~/quality-momentum && python3 us_scan.py --hours 7-11 --hours-tz America/Los_Angeles >> us.log 2>&1
30 * * * * cd ~/quality-momentum && python3 india_scan.py --hours 7-11 --hours-tz Asia/Kolkata >> india.log 2>&1
```

Cron starts each script every hour; `--hours` lets it through only at 7, 8, 9, 10 and 11 AM on weekdays in that
time zone (the India line fires at :30 Seattle time, which is :00 in India all year). The first attempt that
succeeds writes the report; later attempts that morning see the session is done and exit in seconds. A failed
attempt (network, Yahoo outage) changes nothing, so the next hour retries. Use the full path to python3
(`which python3`) if cron can't find it.

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

`data/<market>/`: `universe.csv`, `watchlist.csv` (passed), `watchlist_full.csv` (every stock with every rule
value), `positions.csv`, `signals.csv`, `missed.csv`, `state.json` (last session processed, cooldowns).
