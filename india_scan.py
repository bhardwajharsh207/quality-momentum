"""
India Quality Momentum - daily scan (Nifty 500 universe, NSE).

Run:  python india_scan.py            daily run (Layer A rebuilds itself weekly)
      python india_scan.py --rebuild  force a full watchlist rebuild
      python india_scan.py --force    re-run a session that was already processed
Schedule: 7:00 AM IST on weekdays (processes the previous session's close).
"""
import datetime as dt
import io

import pandas as pd
import requests

from qm_core import Config, cli

NIFTY500_URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]


def load_universe() -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    last_err = None
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df = df[df["Series"].str.strip() == "EQ"] if "Series" in df else df
            return pd.DataFrame({"ticker": df["Symbol"].str.strip() + ".NS",
                                 "sector": df["Industry"].str.strip()})
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Nifty 500 list unavailable: {last_err}")


CONFIG = Config(
    market="india",
    capital=5000,                   # C, in rupees
    n_stocks=3,                     # N
    currency="₹",
    benchmark="^CRSLDX",            # Nifty 500 index on Yahoo
    tz="Asia/Kolkata",
    close_time=dt.time(15, 30),
    min_mcap=5_000 * 1e7,           # ₹5,000 crore
    universe_loader=load_universe,
    probe_tickers=("RELIANCE.NS", "HDFCBANK.NS", "INFY.NS", "TCS.NS"),
    financial_sectors=("Financial Services",),
    cyclical_sectors=("Metals & Mining", "Realty", "Oil Gas & Consumable Fuels"),
    manual_checks=(
        "promoter pledge < 5% and promoter holding not down > 2 pts over 4 quarters (2.8, 2.9)",
        "not in NSE ASM/GSM surveillance (1.3)",
    ),
    research_url="https://www.screener.in/company/{symbol}/",
    mcap_div=1e7,
    mcap_label=" cr",
    # Delivery trades at a discount broker: ~0.24% statutory (STT, exchange, stamp, GST) + ~₹16 DP charge on sell
    cost_pct=0.0024,
    cost_fixed=16,
)

if __name__ == "__main__":
    cli(CONFIG)
