"""
US Quality Momentum - daily scan (S&P 500 universe).

Run:  python us_scan.py            daily run (Layer A rebuilds itself weekly)
      python us_scan.py --rebuild  force a full watchlist rebuild
      python us_scan.py --force    re-run a session that was already processed
Schedule: 7:00 AM US Eastern on weekdays (processes the previous session's close).
"""
import datetime as dt
import io

import pandas as pd
import requests

from qm_core import Config, cli

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def load_universe() -> pd.DataFrame:
    r = requests.get(SP500_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    return pd.DataFrame({"ticker": df["Symbol"].str.strip().str.replace(".", "-", regex=False),
                         "sector": df["GICS Sector"].str.strip()})


CONFIG = Config(
    market="us",
    capital=5000,                   # C, in dollars - set your own
    n_stocks=3,                     # N
    currency="$",
    benchmark="^GSPC",              # S&P 500 index
    tz="America/New_York",
    close_time=dt.time(16, 0),
    min_mcap=5e9,                   # $5B
    universe_loader=load_universe,
    probe_tickers=("AAPL", "MSFT", "JPM", "XOM"),
    financial_sectors=("Financials",),
    cyclical_sectors=("Materials", "Energy", "Real Estate"),
    manual_checks=(),
    research_url="https://finance.yahoo.com/quote/{symbol}/",
    # Commission-free broker; allow ~0.05% for spread
    cost_pct=0.0005,
    cost_fixed=0,
    # Fresh golden cross (4.3) is now a tag ("emerging"), not a filter. 52w range, relative strength and
    # beat-the-index (4.4-4.7) remain entry gates for the US for now.
    trend_extra_gates=True,
)

if __name__ == "__main__":
    cli(CONFIG)
