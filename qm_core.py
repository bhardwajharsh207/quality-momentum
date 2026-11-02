"""
Quality Momentum - shared rules engine.

Implements the "Quality Momentum: Daily Run Spec":
  Layer A  watchlist build      (sections 1-3, 5)  weekly + after a stock reports results
  Layer B  daily signal scan    (sections 4, 6, 7, 8, 11)
  Layer C  daily position check (section 9)
  Output   daily report         (section 12)

Market-specific settings live in india_scan.py and us_scan.py.
Data source: Yahoo Finance via the yfinance package.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    market: str                      # "india" or "us"
    capital: float                   # C
    n_stocks: int                    # N
    currency: str                    # display symbol
    benchmark: str                   # Yahoo ticker of the benchmark index
    tz: str                          # market time zone
    close_time: dt.time              # market close, local time
    min_mcap: float                  # 2.1, in local currency units
    universe_loader: object = None   # callable -> DataFrame[ticker, symbol, sector]
    financial_sectors: tuple = ()
    cyclical_sectors: tuple = ()
    manual_checks: tuple = ()        # rules the data source cannot verify
    research_url: str = ""           # e.g. "https://www.screener.in/company/{symbol}/"
    mcap_div: float = 1e9            # market cap display unit
    mcap_label: str = "B"

    # Risk and execution (0.4, 11.x)
    risk_pct: float = 0.015
    max_stop_pct: float = 0.08
    max_chase_pct: float = 0.05
    disaster_stop_pct: float = 0.12
    max_new_per_day: int = 2
    max_per_sector: int = 2          # 7.5: how many open positions may share a sector
    cooldown_days: int = 20

    # Costs (8.6): estimated round-trip cost = cost_fixed + cost_pct * value
    cost_fixed: float = 0.0
    cost_pct: float = 0.0
    max_cost_pct: float = 0.01

    # Quality gate (2.x)
    roce_avg_min: float = 0.10
    roce_each_min: float = 0.08
    roe_fin_min: float = 0.12
    roa_fin_min: float = 0.012
    sales_cagr_min: float = 0.10
    profit_cagr_min: float = 0.12
    de_max: float = 0.5
    ocf_np_min: float = 0.8

    # Earnings acceleration (3.x)
    q_sales_yoy_min: float = 0.15
    q_profit_yoy_min: float = 0.20
    q_ebitda_yoy_min: float = 0.15

    # Valuation (5.x)
    pe_rel_max: float = 1.3
    peg_max: float = 2.0
    pe_hard_max: float = 100.0

    # Trend (4.x)
    rs_top_pct: float = 0.30
    require_fresh_cross: bool = True  # legacy 4.3 gate; only the backtest's "legacy" variant uses it now
    trend_extra_gates: bool = True    # 4.4-4.7 (52w range, RS, beat index) as gates; False = score inputs only
    emerging_cross_days: int = 30     # 50/200 cross this recent = "emerging"
    emerging_gap_pct: float = 0.03    # 50DMA within this % below the 200DMA = "emerging"
    pullback20: bool = True           # 6B2: allow pullback entries to the 20DMA
    entry_cross_days: int | None = None  # entries need a 50/200 cross within N sessions (None = any trend age)
    watchlist_tab_top: int = 25       # Board: full write-up (why / vs peers / next) for the top N by score

    # Housekeeping
    watchlist_max_age_days: int = 7
    strict_missing: bool = False     # True: missing fundamental data fails a rule
    workers: int = 2
    refetch_cap: int = 150           # max no-data stocks re-fetched per daily run
    probe_tickers: tuple = ()        # liquid stocks used to date the latest session
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data" / self.market

    @property
    def report_dir(self) -> Path:
        return self.base_dir / "reports" / self.market


# --------------------------------------------------------------------------- #
# Data source (swap for a mock in tests)
# --------------------------------------------------------------------------- #


class YahooSource:
    name = "Yahoo Finance (yfinance)"

    def __init__(self):
        import yfinance as yf
        self.yf = yf

    def prices(self, tickers: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            df = self.yf.download(chunk, period=period, auto_adjust=True, group_by="ticker",
                                  threads=True, progress=False)
            if df is None or df.empty:
                continue
            multi = isinstance(df.columns, pd.MultiIndex)
            for t in chunk:
                try:
                    d = df[t] if multi else df
                except KeyError:
                    continue
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                if len(d):
                    d.index = pd.to_datetime(d.index).tz_localize(None)
                    out[t] = d
        return out

    def fundamentals(self, ticker: str) -> dict:
        tk = self.yf.Ticker(ticker)
        f = {"info": {}, "annual_is": None, "annual_bs": None, "annual_cf": None,
             "q_is": None, "next_earnings": None}
        for key, attr in [("info", "info"), ("annual_is", "income_stmt"),
                          ("annual_bs", "balance_sheet"), ("annual_cf", "cashflow"),
                          ("q_is", "quarterly_income_stmt")]:
            for wait in (0, 5, 20, 60):         # back off when Yahoo rate-limits
                time.sleep(wait)
                try:
                    val = getattr(tk, attr)
                except Exception:
                    continue
                empty = (not val) if key == "info" else (val is None or getattr(val, "empty", True))
                f[key] = val
                if not empty:
                    break
        try:
            cal = tk.calendar or {}
            dates = cal.get("Earnings Date") or []
            if dates:
                f["next_earnings"] = pd.Timestamp(min(dates)).date()
        except Exception:
            pass
        return f


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _row(df, names) -> pd.Series:
    """First matching line item from a yfinance statement, oldest -> newest."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce").dropna()
            s.index = pd.to_datetime(s.index)
            return s.sort_index()
    return pd.Series(dtype=float)


def _cagr(s: pd.Series):
    if len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return None
    years = (s.index[-1] - s.index[0]).days / 365.25
    return (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1 if years > 0.5 else None


def _yoy(q: pd.Series) -> list:
    """YoY growth for each quarter that has a quarter 4 periods earlier."""
    out = []
    for i in range(4, len(q)):
        base = q.iloc[i - 4]
        out.append(q.iloc[i] / base - 1 if base > 0 else None)
    return out


def _rule(rule, name, value, threshold, ok, gate=True):
    if isinstance(value, (np.floating, np.integer)):
        value = float(value)
    r = {"rule": rule, "name": name, "value": value, "threshold": threshold,
         "ok": None if ok is None else bool(ok)}
    if not gate:
        r["gate"] = False
    return r


def _fmt(v, pct=False):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float, np.floating)):
        return f"{v * 100:.1f}%" if pct else f"{v:,.2f}"
    return str(v)


CORE_RULES = {"2.1", "2.2", "2.4", "2.5", "3.1", "3.2"}   # missing data on these = fail
PCT_RULES = {"2.2", "2.2b", "2.3", "2.3/2.10", "2.4", "2.5", "3.1", "3.2", "3.3", "3.4"}


def _passes(rules, strict_missing):
    for r in rules:
        if r["ok"] is False:
            return False
        if r["ok"] is None and (strict_missing or r["rule"] in CORE_RULES):
            return False
    return True


def _unverified(rules):
    return [r["rule"] for r in rules if r["ok"] is None]


def _gates(rules):
    return [r for r in rules if r.get("gate", True)]


def fund_summary(rules) -> dict:
    """Fundamentals are scored, not gated: how many rules pass, and which ones miss."""
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except Exception:
            rules = None
    if not isinstance(rules, list) or not rules:
        return {"has_fund": False, "fund_ok": 0, "fund_known": 0, "fund_rate": np.nan, "fund_misses": ""}
    known = [r for r in rules if r["ok"] is not None]
    ok = sum(1 for r in known if r["ok"])
    return {"has_fund": True, "fund_ok": ok, "fund_known": len(known),
            "fund_rate": ok / len(known) if known else np.nan,
            "fund_misses": ",".join(r["rule"] for r in rules if r["ok"] is False)}


def apply_fund_summary(wl: pd.DataFrame) -> pd.DataFrame:
    """Adds score inputs from each stock's rule results. `passed` now means: fundamentals are on file."""
    if not len(wl):
        return wl
    wl = wl.copy()
    rules = wl["rules"] if "rules" in wl else pd.Series([None] * len(wl), index=wl.index)
    summ = pd.DataFrame([fund_summary(r) for r in rules], index=wl.index)
    for col in summ:
        wl[col] = summ[col]
    wl["passed"] = summ["has_fund"]
    return wl


# --------------------------------------------------------------------------- #
# Layer A - fundamentals (sections 2, 3, 5 inputs)
# --------------------------------------------------------------------------- #


def evaluate_fundamentals(cfg: Config, ticker: str, sector: str, f: dict,
                          px: pd.DataFrame | None) -> dict:
    info = f.get("info") or {}
    is_fin = sector in cfg.financial_sectors or info.get("sector") == "Financial Services"
    is_cyc = sector in cfg.cyclical_sectors or info.get("sector") in ("Basic Materials", "Energy", "Real Estate") \
        or "airline" in str(info.get("industry", "")).lower()
    rules = []

    # 2.1 market cap
    mcap = info.get("marketCap")
    rules.append(_rule("2.1", "Market cap", mcap, cfg.min_mcap,
                       None if mcap is None else mcap > cfg.min_mcap))

    ais, abs_, acf = f.get("annual_is"), f.get("annual_bs"), f.get("annual_cf")
    revenue = _row(ais, ["Total Revenue", "Operating Revenue"])
    profit = _row(ais, ["Net Income", "Net Income Common Stockholders"])
    ebit = _row(ais, ["EBIT", "Operating Income"])
    assets = _row(abs_, ["Total Assets"])
    cur_liab = _row(abs_, ["Current Liabilities"])
    equity = _row(abs_, ["Stockholders Equity", "Common Stock Equity"])
    debt = _row(abs_, ["Total Debt"])
    ocf = _row(acf, ["Operating Cash Flow"])
    years = len(profit)

    roce_series = None
    if is_fin:
        # 2.2 (financials): ROE and ROA averages
        roe = (profit / equity).dropna()
        roa = (profit / assets).dropna()
        roe_avg = float(roe.mean()) if len(roe) else None
        roa_avg = float(roa.mean()) if len(roa) else None
        rules.append(_rule("2.2", "Avg ROE (financial)", roe_avg, cfg.roe_fin_min,
                           None if roe_avg is None else roe_avg > cfg.roe_fin_min))
        rules.append(_rule("2.2b", "Avg ROA (financial)", roa_avg, cfg.roa_fin_min,
                           None if roa_avg is None else roa_avg > cfg.roa_fin_min))
        quality_metric = roe_avg
    else:
        cap_employed = (assets - cur_liab).dropna()
        roce_series = (ebit / cap_employed).dropna()
        roce_avg = float(roce_series.mean()) if len(roce_series) else None
        rules.append(_rule("2.2", f"Avg ROCE ({len(roce_series)}y)", roce_avg, cfg.roce_avg_min,
                           None if roce_avg is None else roce_avg > cfg.roce_avg_min))
        last3 = roce_series.iloc[-3:]
        each_min = cfg.roce_avg_min if is_cyc else cfg.roce_each_min   # 2.10 stricter for cyclicals
        ok3 = None if len(last3) < 3 else bool((last3 > each_min).all())
        rules.append(_rule("2.3" + ("/2.10" if is_cyc else ""), "ROCE each of last 3y",
                           None if last3.empty else float(last3.min()), each_min, ok3))
        quality_metric = roce_avg

    # 2.4 / 2.5 growth (yfinance usually carries 4 annual periods -> ~3y CAGR)
    s_cagr, p_cagr = _cagr(revenue), _cagr(profit)
    rules.append(_rule("2.4", f"Sales CAGR ({max(len(revenue) - 1, 0)}y)", s_cagr, cfg.sales_cagr_min,
                       None if s_cagr is None else s_cagr > cfg.sales_cagr_min))
    rules.append(_rule("2.5", f"Profit CAGR ({max(len(profit) - 1, 0)}y)", p_cagr, cfg.profit_cagr_min,
                       None if p_cagr is None else p_cagr > cfg.profit_cagr_min))

    # 2.6 leverage (skipped for financials)
    de = None
    if not is_fin:
        if len(debt) and len(equity) and equity.iloc[-1] > 0:
            de = float(debt.iloc[-1] / equity.iloc[-1])
        elif len(equity) and equity.iloc[-1] > 0 and not len(debt):
            de = 0.0
        rules.append(_rule("2.6", "Debt / Equity", de, cfg.de_max, None if de is None else de < cfg.de_max))

    # 2.7 cash conversion
    ocf_np = None
    common = ocf.index.intersection(profit.index)
    if len(common) and profit.loc[common].sum() > 0:
        ocf_np = float(ocf.loc[common].sum() / profit.loc[common].sum())
    rules.append(_rule("2.7", "OCF / Net profit (cum.)", ocf_np, cfg.ocf_np_min,
                       None if ocf_np is None else ocf_np > cfg.ocf_np_min))

    # Section 3 - quarterly acceleration
    q = f.get("q_is")
    q_rev = _row(q, ["Total Revenue", "Operating Revenue"])
    q_np = _row(q, ["Net Income", "Net Income Common Stockholders"])
    q_ebitda = _row(q, ["EBITDA", "Normalized EBITDA", "Operating Income"])
    rev_yoy, np_yoy, ebitda_yoy = _yoy(q_rev), _yoy(q_np), _yoy(q_ebitda)
    last = lambda xs: xs[-1] if xs else None
    rules.append(_rule("3.1", "Qtr sales YoY", last(rev_yoy), cfg.q_sales_yoy_min,
                       None if last(rev_yoy) is None else last(rev_yoy) > cfg.q_sales_yoy_min))
    rules.append(_rule("3.2", "Qtr profit YoY", last(np_yoy), cfg.q_profit_yoy_min,
                       None if last(np_yoy) is None else last(np_yoy) > cfg.q_profit_yoy_min))
    accel = None
    if len(np_yoy) >= 3 and None not in np_yoy[-3:]:
        accel = np_yoy[-1] - (np_yoy[-2] + np_yoy[-3]) / 2
    rules.append(_rule("3.3", "Profit growth acceleration", accel, 0.0,
                       None if accel is None else accel >= 0))
    if not is_fin:
        rules.append(_rule("3.4", "Qtr EBITDA YoY", last(ebitda_yoy), cfg.q_ebitda_yoy_min,
                           None if last(ebitda_yoy) is None else last(ebitda_yoy) > cfg.q_ebitda_yoy_min))
    decel_2q = None
    if len(np_yoy) >= 3 and None not in np_yoy[-3:]:
        decel_2q = np_yoy[-1] < np_yoy[-2] < np_yoy[-3]

    # Section 5 inputs - EPS and own historical P/E
    eps_ttm = info.get("trailingEps")
    eps_annual = _row(ais, ["Diluted EPS", "Basic EPS"])
    median_pe = None
    if px is not None and len(eps_annual):
        pes = []
        for d, eps in eps_annual.items():
            if eps > 0:
                window = px["Close"].loc[:d]
                if len(window):
                    pes.append(window.iloc[-1] / eps)
        if len(pes) >= 2:
            median_pe = float(np.median(pes))
    p_cagr_3y = _cagr(profit.iloc[-4:]) if len(profit) >= 2 else None

    return {
        "ticker": ticker, "sector": sector, "is_financial": is_fin, "is_cyclical": is_cyc,
        "no_data": not info.get("marketCap") and years == 0,
        "passed": True, "unverified": ",".join(_unverified(rules)),
        "quality_metric": quality_metric, "ocf_np": ocf_np, "de": de,
        "q_sales_yoy": last(rev_yoy), "q_profit_yoy": last(np_yoy), "accel": accel,
        "decel_2q": decel_2q, "eps_ttm": eps_ttm, "median_pe": median_pe,
        "profit_cagr_3y": p_cagr_3y, "years_of_data": years,
        "next_earnings": f.get("next_earnings"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rules": json.dumps(rules, default=str),
    }


def build_watchlist(cfg: Config, src, universe: pd.DataFrame, prices: dict,
                    only: list[str] | None = None, log=print) -> pd.DataFrame:
    """Layer A. Evaluates the universe (or `only` these tickers) and returns all evaluations."""
    rows = universe if only is None else universe[universe.ticker.isin(only)]
    # 1.2 history; 1.3 surveillance lists are a manual check (not in Yahoo data)
    rows = rows[rows.ticker.map(lambda t: t in prices and len(prices[t]) >= 260)]
    log(f"[Layer A] evaluating fundamentals for {len(rows)} stocks ...")
    results = []

    def work(r):
        time.sleep(0.5)
        try:
            f = src.fundamentals(r.ticker)
            return evaluate_fundamentals(cfg, r.ticker, r.sector, f, prices.get(r.ticker))
        except Exception as e:  # keep going; record the failure
            return {"ticker": r.ticker, "sector": r.sector, "passed": False, "no_data": True,
                    "unverified": f"error: {e}"}

    with cf.ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        for i, res in enumerate(ex.map(work, rows.itertuples(index=False)), 1):
            results.append(res)
            if i % 50 == 0:
                log(f"  {i}/{len(rows)}")
    return pd.DataFrame(results)


# --------------------------------------------------------------------------- #
# Layer B - technicals (sections 4, 5, 6)
# --------------------------------------------------------------------------- #


STAGE_LABEL = {"confirmed": "Confirmed uptrend", "emerging": "Emerging", "pullback": "Pullback in uptrend",
               "stalling": "Stalling", "downtrend": "Downtrend", "": "Not enough history"}
UPTREND = ("confirmed", "emerging")


def trend_stages(c: pd.DataFrame, cfg: Config) -> dict:
    """Classify every stock on every day (columns = stocks). Shared by the daily scan and the backtest.

    confirmed: close > 50DMA > 200DMA, 50DMA rising (vs 10d), 200DMA rising (vs 20d), cross older than N sessions
    emerging:  close above a rising 50DMA and above the 200DMA, and the 50DMA either crossed the 200DMA
               in the last N sessions, sits within a few % below it, or is above it while the 200DMA is still flat
    pullback:  50DMA > 200DMA, both rising, close dipped to between the 200DMA and the 50DMA
    stalling:  50DMA > 200DMA but none of the above (usually the 50DMA has flattened)
    downtrend: everything else
    """
    s20 = c.rolling(20, min_periods=20).mean()
    s50 = c.rolling(50, min_periods=50).mean()
    s200 = c.rolling(200, min_periods=200).mean()
    r50 = s50 > s50.shift(10)
    r200 = s200 > s200.shift(20)
    valid = s200.shift(20).notna() & s50.shift(10).notna()
    above = s50 > s200
    cross = above & ~above.shift(1, fill_value=False) & s200.shift(1).notna()
    pos = np.arange(len(c), dtype=float)[:, None] * np.ones((1, c.shape[1]))
    last = pd.DataFrame(np.where(cross.values, pos, np.nan), index=c.index, columns=c.columns).ffill()
    age = (pd.DataFrame(pos, index=c.index, columns=c.columns) - last).where(above)
    fresh = above & age.le(cfg.emerging_cross_days)
    above50 = c > s50
    confirmed = above50 & above & r50 & r200 & ~fresh
    emerging = above50 & (c > s200) & r50 & ((~above & (s50 >= (1 - cfg.emerging_gap_pct) * s200)) |
                                              fresh | (above & ~r200))
    pullback = above & r50 & r200 & (c <= s50) & (c > s200)
    stalling = above
    stage = pd.DataFrame(np.select([~valid.values, confirmed.values, emerging.values, pullback.values, stalling.values],
                                   ["", "confirmed", "emerging", "pullback", "stalling"], "downtrend"),
                         index=c.index, columns=c.columns)
    return {"stage": stage, "s20": s20, "s50": s50, "s200": s200, "r50": r50, "r200": r200,
            "cross_age": age, "fresh": fresh}


IN_UPTREND = ("confirmed", "emerging", "pullback")    # stages shown on the Board


def stage_history(s: pd.Series) -> dict:
    """Sessions in the current stage, and in an uptrend (emerging/confirmed/pullback, brief stalls included)."""
    s = s[s != ""]
    out = {"stage_since": None, "days_in_stage": None, "up_since": None, "days_up": None}
    if s.empty:
        return out
    chg = np.flatnonzero(s.ne(s.shift()).values)
    i = chg[-1]
    out.update(stage_since=s.index[i].date(), days_in_stage=int(len(s) - i))
    up = s.isin(IN_UPTREND + ("stalling",))      # a brief stall (50DMA flat, still above 200DMA) doesn't reset it
    if s.iloc[-1] in IN_UPTREND:
        j = np.flatnonzero(up.ne(up.shift()).values)[-1]
        out.update(up_since=s.index[j].date(), days_up=int(len(s) - j))
    return out


def stage_events(s: pd.Series, after) -> list[dict]:
    """Stage changes on sessions after `after` that involve an uptrend stage."""
    s = s[s != ""]
    chg = np.flatnonzero(s.ne(s.shift()).values)
    out = []
    for k in range(1, len(chg)):
        i = chg[k]
        d = s.index[i].date()
        frm, to = s.iloc[i - 1], s.iloc[i]
        if d <= after or (frm not in IN_UPTREND and to not in IN_UPTREND):
            continue
        out.append({"date": d, "from": frm, "to": to, "days_prev": int(i - chg[k - 1])})
    return out


def event_label(frm: str, to: str) -> str:
    if frm not in IN_UPTREND:
        return f"Entered uptrend ({STAGE_LABEL[to]})"
    if to not in IN_UPTREND:
        return f"Left uptrend (now {STAGE_LABEL[to].lower()})"
    return f"{STAGE_LABEL[frm]} -> {STAGE_LABEL[to]}"


def trend_rules(d: pd.DataFrame, rs_pct: float | None, ret63: float, bench63: float, cfg: Config):
    c, lo, hi = d["Close"], d["Low"], d["High"]
    T = trend_stages(c.to_frame("x"), cfg)
    g = lambda k, i=-1: T[k]["x"].iloc[i]
    stage, s20, s50, s200 = g("stage"), g("s20"), g("s50"), g("s200")
    close = float(c.iloc[-1])
    age = g("cross_age")
    lo52, hi52 = lo.iloc[-252:].min(), hi.iloc[-252:].max()
    if s50 > s200:
        cross_txt = (f"crossed {int(age)} sessions ago" if not pd.isna(age) else "above for 1y+")
    else:
        cross_txt = f"50DMA {s50 / s200 - 1:+.1%} vs 200DMA"
    info = {"trend_stage": stage, "s20": float(s20), "s50": float(s50), "s200": float(s200),
            "s200_20": float(T["s200"]["x"].iloc[-21]), "cross_txt": cross_txt,
            "cross_age": None if pd.isna(age) else int(age), "fresh_cross": bool(g("fresh")),
            "ext20": close / s20 - 1, "ext50": close / s50 - 1, "from_hi52": close / hi52,
            "from_lo52": close / lo52, **stage_history(T["stage"]["x"]), "stage_series": T["stage"]["x"]}
    xg = cfg.trend_extra_gates
    rules = [
        _rule("4.1", "Trend stage confirmed or emerging", STAGE_LABEL.get(stage, stage), "confirmed/emerging",
              stage in UPTREND),
        _rule("4.2", "50DMA rising (vs 10d ago)", f"{s50:.2f} vs {T['s50']['x'].iloc[-11]:.2f}", "higher",
              bool(g("r50")), gate=False),
        _rule("4.2b", "200DMA rising (vs 20d ago)", f"{s200:.2f} vs {info['s200_20']:.2f}", "higher",
              bool(g("r200")), gate=False),
        (_rule("4.3", "50/200 golden cross recent (entry gate)", cross_txt, f"<= {cfg.entry_cross_days} sessions",
               info["cross_age"] is not None and info["cross_age"] <= cfg.entry_cross_days)
         if cfg.entry_cross_days else
         _rule("4.3", "50/200 cross (fresh = tag, not a filter)", cross_txt, f"<= {cfg.emerging_cross_days} sessions",
               info["fresh_cross"], gate=False)),
        _rule("4.4", "Close / 52w low", close / lo52, 1.30, bool(close >= 1.30 * lo52), gate=xg),
        _rule("4.5", "Close / 52w high", close / hi52, 0.75, bool(close >= 0.75 * hi52), gate=xg),
        _rule("4.6", "6m return percentile", rs_pct, 1 - cfg.rs_top_pct,
              None if rs_pct is None else rs_pct >= 1 - cfg.rs_top_pct, gate=xg),
        _rule("4.7", "3m return vs benchmark", f"{ret63:.1%} vs {bench63:.1%}", "higher", bool(ret63 > bench63),
              gate=xg),
    ]
    return rules, T["s50"]["x"], info


def valuation_rules(price: float, w: dict, cfg: Config):
    eps = w.get("eps_ttm")
    pe = price / eps if eps and eps > 0 else None
    med = w.get("median_pe")
    g = w.get("profit_cagr_3y")
    rel = pe / med if pe and med and med > 0 else None
    peg = pe / (g * 100) if pe and g and g > 0 else None
    hard_fail = pe is None or pe > cfg.pe_hard_max
    ok_51 = None if rel is None else rel <= cfg.pe_rel_max
    ok_52 = None if peg is None else peg < cfg.peg_max
    if hard_fail:
        ok = False
    elif ok_51 is None and ok_52 is None:
        ok = None
    else:
        ok = bool(ok_51) or bool(ok_52)
    return [
        _rule("5.3", "P/E (hard cap, positive EPS)", pe, cfg.pe_hard_max, not hard_fail),
        _rule("5.1", "P/E vs own median", rel, cfg.pe_rel_max, ok_51),
        _rule("5.2", "PEG", peg, cfg.peg_max, ok_52),
        _rule("5", "Valuation (5.1 or 5.2)", None, None, ok),
    ]


def find_base(d: pd.DataFrame):
    """6A.1: longest 30-100 day window (excluding today) whose range <= 25% of its high."""
    best = None
    for length in range(30, 101, 5):
        if len(d) < length + 1:
            break
        w = d.iloc[-1 - length:-1]
        hi, lo = w["High"].max(), w["Low"].min()
        if (hi - lo) / hi <= 0.25:
            best = {"length": length, "pivot": float(w["Close"].max()), "base_low": float(lo)}
    return best


def entry_setup(d: pd.DataFrame, s50: pd.Series, cfg: Config):
    c, v = d["Close"], d["Volume"]
    vol50 = v.iloc[-51:-1].mean()
    # 6A breakout
    base = find_base(d)
    if base:
        piv = base["pivot"]
        if c.iloc[-1] > piv and c.iloc[-1] <= piv * (1 + cfg.max_chase_pct) and v.iloc[-1] > 1.5 * vol50:
            return {"setup": "breakout", "structure_low": base["base_low"],
                    "limit": piv * (1 + cfg.max_chase_pct),
                    "detail": f"{base['length']}d base, pivot {piv:.2f}, vol {v.iloc[-1] / vol50:.1f}x avg"}
    # 6B pullback to the 50DMA (last 5 sessions before today)
    w = d.iloc[-6:-1]
    near = (w["Low"] <= s50.iloc[-6:-1] * 1.03).any()
    dry = w["Volume"].mean() < vol50
    bounce = c.iloc[-1] > d["High"].iloc[-2]
    if near and dry and bounce:
        return {"setup": "pullback", "structure_low": float(w["Low"].min()),
                "limit": c.iloc[-1] * (1 + cfg.max_chase_pct),
                "detail": f"touched 50DMA zone, pullback vol {w['Volume'].mean() / vol50:.2f}x avg, closed above prior high"}
    # 6B2 pullback to a rising 20DMA (strong trends often never reach the 50DMA)
    if cfg.pullback20:
        s20 = c.rolling(20).mean()
        w3 = d.iloc[-4:-1]
        near20 = (w3["Low"] <= s20.iloc[-4:-1] * 1.01).any()
        rising20 = s20.iloc[-1] > s20.iloc[-6]
        if near20 and rising20 and bounce and c.iloc[-1] > s20.iloc[-1] and w3["Volume"].mean() < vol50:
            return {"setup": "pullback20", "structure_low": float(w["Low"].min()),
                    "limit": c.iloc[-1] * (1 + cfg.max_chase_pct),
                    "detail": f"dipped to rising 20DMA, pullback vol {w3['Volume'].mean() / vol50:.2f}x avg, "
                              "closed above prior high"}
    return None


# --------------------------------------------------------------------------- #
# Ranking and sizing (sections 7, 8)
# --------------------------------------------------------------------------- #


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    """All fundamentals are score inputs (no fundamental gate). Percentiles are across the whole universe."""
    df = df.copy()
    for col in ["quality_metric", "ocf_np", "de", "q_sales_yoy", "q_profit_yoy", "accel", "fund_rate",
                "ret126", "excess63", "from_hi52"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    pr = lambda s, asc=True: s.rank(pct=True, ascending=asc).fillna(0.5)
    rate = df["fund_rate"].fillna(0.5)
    q = (pr(df["quality_metric"]) + pr(df["ocf_np"]) + pr(df["de"], asc=False) + rate) / 4
    g = (pr(df["q_sales_yoy"]) + pr(df["q_profit_yoy"]) + pr(df["accel"])) / 3
    m = (pr(df["ret126"]) + pr(df["excess63"]) + pr(df["from_hi52"])) / 3
    df = df.copy()
    df["score_quality"], df["score_growth"], df["score_momentum"] = q, g, m
    df["score"] = 0.4 * q + 0.3 * g + 0.3 * m
    return df


def size_position(cfg: Config, entry: float, structure_low: float):
    stop = max(structure_low, entry * (1 - cfg.max_stop_pct))    # 8.2 tighter of the two
    if stop >= entry:
        stop = entry * (1 - cfg.max_stop_pct)
    risk_sh = cfg.capital * cfg.risk_pct / (entry - stop)
    cap_sh = (cfg.capital / cfg.n_stocks) / entry
    shares = int(math.floor(min(risk_sh, cap_sh)))
    value = shares * entry
    cost = cfg.cost_fixed + cfg.cost_pct * value if shares else 0
    reason = None
    if shares < 1:
        reason = "position size below 1 share"
    elif cost / value > cfg.max_cost_pct:
        reason = f"est. round-trip cost {cost / value:.1%} > {cfg.max_cost_pct:.0%}"
    return {"stop": stop, "shares": shares, "value": value, "est_cost": cost,
            "risk_shares": risk_sh, "cap_shares": cap_sh, "skip_reason": reason}


# --------------------------------------------------------------------------- #
# State and files
# --------------------------------------------------------------------------- #

POSITION_COLS = ["ticker", "entry_date", "entry_price", "shares", "stop", "note"]


def load_state(cfg):
    p = cfg.data_dir / "state.json"
    return json.loads(p.read_text()) if p.exists() else {"last_bar_date": None, "cooldowns": {}}


def save_state(cfg, state):
    (cfg.data_dir / "state.json").write_text(json.dumps(state, indent=2, default=str))


def load_positions(cfg) -> pd.DataFrame:
    p = cfg.data_dir / "positions.csv"
    if not p.exists():
        pd.DataFrame(columns=POSITION_COLS).to_csv(p, index=False)
    df = pd.read_csv(p, dtype={"ticker": str, "note": str})
    for col in POSITION_COLS:
        if col not in df:
            df[col] = None
    return df


def append_csv(path: Path, rows: list[dict], key_cols: list[str]):
    if not rows:
        return
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path, dtype=str)
        new = pd.concat([old, new.astype(str)]).drop_duplicates(subset=key_cols, keep="last")
    new.to_csv(path, index=False)


# --------------------------------------------------------------------------- #
# Main daily run
# --------------------------------------------------------------------------- #


def _drop_unfinished_bar(cfg, px: dict) -> dict:
    now = dt.datetime.now(ZoneInfo(cfg.tz))
    if now.time() >= cfg.close_time:
        return px
    today = pd.Timestamp(now.date())
    return {t: d[d.index < today] for t, d in px.items()}


def run(cfg: Config, src=None, rebuild=False, force=False, log=print) -> Path | None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    src = src or YahooSource()
    fetched_at = dt.datetime.now(ZoneInfo(cfg.tz)).strftime("%Y-%m-%d %H:%M %Z")
    state = load_state(cfg)

    # Ages come from state.json, not file times (a fresh git checkout resets file times)
    def age_days(key):
        stamp = state.get(key)
        return 999 if not stamp else (dt.date.today() - dt.date.fromisoformat(stamp)).days

    # Universe (0.2), refreshed weekly
    uni_path = cfg.data_dir / "universe.csv"
    if rebuild or not uni_path.exists() or age_days("universe_loaded") >= cfg.watchlist_max_age_days:
        try:
            universe = cfg.universe_loader()
            universe.to_csv(uni_path, index=False)
            state["universe_loaded"] = str(dt.date.today())
        except Exception as e:
            if not uni_path.exists():
                raise RuntimeError(f"Could not load universe and no cache exists: {e}")
            log(f"Universe download failed ({e}); using cached list.")
    universe = pd.read_csv(uni_path, dtype=str)
    probes = list(cfg.probe_tickers) or list(universe.ticker[:5])

    # Quick check on a few liquid stocks, so retry attempts after a good run exit in seconds
    if not force and state.get("last_bar_date"):
        b = _drop_unfinished_bar(cfg, src.prices(probes, period="1mo"))
        last = max((d.index[-1].date() for d in b.values()), default=None)
        if last is not None and str(last) == state["last_bar_date"]:
            log(f"No new session since {last}; nothing to do (use --force to re-run).")
            return None

    # Prices for universe + benchmark + held positions
    positions = load_positions(cfg)
    tickers = sorted(set(universe.ticker) | set(positions.ticker.dropna()) | {cfg.benchmark})
    log(f"Downloading prices for {len(tickers)} tickers ...")
    px = _drop_unfinished_bar(cfg, src.prices(tickers))
    if cfg.benchmark not in px:
        raise RuntimeError(f"No benchmark data for {cfg.benchmark}")
    bench = px[cfg.benchmark]

    # Session date = the most common latest date across the universe (index feeds can lag)
    last_dates = pd.Series([px[t].index[-1] for t in universe.ticker if t in px])
    if last_dates.empty:
        raise RuntimeError("No price data for the universe")
    log("Latest bar dates across universe: " +
        ", ".join(f"{k.date()}: {v}" for k, v in last_dates.value_counts().sort_index().tail(4).items()))
    ref_ts = last_dates.mode().max()
    bar_date = ref_ts.date()
    ref = next(px[t] for t in probes + list(universe.ticker) if t in px and px[t].index[-1] == ref_ts)
    bars = ref.index

    # Never go backwards: stale data from Yahoo must not overwrite a newer session
    last_done = state.get("last_bar_date")
    if last_done and bar_date < dt.date.fromisoformat(last_done):
        log(f"WARNING: fetched data ends {bar_date}, older than last processed session {last_done}. "
            "Yahoo returned stale data; skipping (nothing to overwrite).")
        return None

    # 0.7 / 0.8 idempotence and calendar
    if state.get("last_bar_date") == str(bar_date) and not force:
        log(f"No new session since {bar_date}; nothing to do (use --force to re-run).")
        return None
    notes = []
    stale_days = (dt.datetime.now(ZoneInfo(cfg.tz)).date() - bar_date).days
    if stale_days > 4:
        notes.append(f"Latest data is {stale_days} days old; check for a data delay.")
    if bench.index[-1].date() < bar_date:
        notes.append(f"Benchmark {cfg.benchmark} data ends {bench.index[-1].date()}; "
                     "relative-strength rule 4.7 uses that date.")
    stale_note = " ".join(notes)

    # ---------------- Layer A ---------------- #
    wl_path = cfg.data_dir / "watchlist_full.csv"
    need_full = rebuild or not wl_path.exists() or age_days("watchlist_built") >= cfg.watchlist_max_age_days
    old_wl = pd.read_csv(wl_path) if wl_path.exists() else pd.DataFrame()
    if need_full:
        fresh = build_watchlist(cfg, src, universe, px, log=log)
        # If Yahoo returned nothing for a stock this time, keep its last good evaluation
        if len(old_wl) and "no_data" in old_wl:
            keep = old_wl[(old_wl.no_data != True) &
                          old_wl.ticker.isin(fresh.ticker[fresh.no_data == True])]
            fresh = pd.concat([fresh[~fresh.ticker.isin(keep.ticker)], keep], ignore_index=True)
        wl = fresh
        state["watchlist_built"] = str(dt.date.today())
    else:
        wl = old_wl
        # Refresh stocks that just reported results, plus stocks Yahoo returned no data for
        due = []
        if "next_earnings" in wl and len(wl):
            ne = pd.to_datetime(wl["next_earnings"], errors="coerce").dt.normalize()
            fa = pd.to_datetime(wl["fetched_at"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
            due = list(wl.ticker[(ne <= pd.Timestamp(bar_date)) & (fa <= ne)])
        if "no_data" in wl:
            due += list(wl.ticker[wl.no_data == True])[:cfg.refetch_cap]
        due = sorted(set(due))
        if due:
            log(f"[Layer A] refreshing {len(due)} stocks (new results or missing data) ...")
            fresh = build_watchlist(cfg, src, universe, px, only=due, log=log)
            keep_old = wl[wl.ticker.isin(fresh.ticker[fresh.no_data == True]) & (wl.get("no_data") != True)]
            fresh = fresh[~fresh.ticker.isin(keep_old.ticker)]
            wl = pd.concat([wl[~wl.ticker.isin(fresh.ticker)], fresh], ignore_index=True)
    wl = apply_fund_summary(wl)
    wl.to_csv(wl_path, index=False)
    passed = wl[wl.passed == True].copy()        # = fundamentals on file (scored, not gated)
    # watchlist.csv = strong fundamentals (>= 75% of rules pass); the backtest uses it as its look-ahead list
    strong = passed[pd.to_numeric(passed["fund_rate"], errors="coerce") >= 0.75] if len(passed) else passed
    strong.drop(columns=["rules"], errors="ignore").to_csv(cfg.data_dir / "watchlist.csv", index=False)

    # ---------------- Layer C: positions ---------------- #
    exits, stop_updates, held_notes = [], [], []
    open_pos = positions.dropna(subset=["ticker"])
    for i, p in open_pos.iterrows():
        t = p.ticker
        d = px.get(t)
        if d is None or len(d) < 60:
            held_notes.append(f"{t}: no price data; check manually.")
            continue
        close = float(d["Close"].iloc[-1])
        s50 = d["Close"].rolling(50).mean().iloc[-1]
        entry, stop = float(p.entry_price), float(p.stop)
        days_held = int((d.index > pd.Timestamp(p.entry_date)).sum())
        reason = None
        if close <= stop:
            reason = "9.1 stop hit (close at or below stop)" if stop < entry else "9.1 breakeven stop hit"
        if not reason:
            fund = wl[wl.ticker == t]
            if len(fund) and fund.iloc[0].get("decel_2q") in (True, "True"):
                reason = "9.4 profit growth decelerated 2 quarters in a row"
        if not reason and stop >= entry and bar_date.weekday() == 4 and close < s50:
            reason = f"9.3 weekly close below 50DMA ({close:.2f} < {s50:.2f})"
        if not reason and days_held >= 40 and close < entry * 1.05:
            reason = f"9.5 time stop: {days_held} sessions, gain {close / entry - 1:.1%} < 5%"
        if reason:
            exits.append({"ticker": t, "close": close, "entry": entry,
                          "pnl_pct": close / entry - 1, "reason": reason})
            if reason.startswith("9.1") and stop < entry:
                state["cooldowns"][t] = str(bar_date)
            continue
        if close >= entry * 1.15 and stop < entry:
            positions.loc[i, "stop"] = entry
            stop_updates.append({"ticker": t, "old": stop, "new": entry,
                                 "why": f"9.2 close {close:.2f} is +{close / entry - 1:.0%}; stop to breakeven"})
        held_notes.append(f"{t}: close {close:.2f}, {close / entry - 1:+.1%}, stop "
                          f"{float(positions.loc[i, 'stop']):.2f}, {days_held} sessions held")
    positions.to_csv(cfg.data_dir / "positions.csv", index=False)

    # ---------------- Layer B: trend on the whole universe, then signals ---------------- #
    ret = lambda d, n: d["Close"].iloc[-1] / d["Close"].iloc[-1 - n] - 1 if len(d) > n else np.nan
    uni_ret126 = pd.Series({t: ret(px[t], 126) for t in universe.ticker if t in px}).dropna()
    pct126 = uni_ret126.rank(pct=True)
    bench63 = ret(bench, 63)
    wl_map = {r["ticker"]: r for r in wl.to_dict("records")} if len(wl) else {}
    sector_of = dict(zip(universe.ticker, universe.sector))

    rows, trend = [], {}
    for t in universe.ticker:
        d = px.get(t)
        if d is None or len(d) < 260:
            continue
        r126, r63 = ret(d, 126), ret(d, 63)
        t_rules, s50, info = trend_rules(d, pct126.get(t), r63, bench63, cfg)
        trend[t] = (t_rules, s50, info)
        w = dict(wl_map.get(t) or {"ticker": t, "sector": sector_of.get(t, ""), "passed": False})
        w.update({"ret126": r126, "excess63": r63 - bench63, "from_hi52": info["from_hi52"]})
        rows.append(w)
    scored = add_scores(pd.DataFrame(rows)) if rows else pd.DataFrame()
    score_cols = ["score", "score_quality", "score_growth", "score_momentum"]
    score_map = scored.set_index("ticker")[score_cols].to_dict("index") if len(scored) else {}

    # Stage changes since the last processed session (all missed sessions, capped at 10), from price history
    in_trend = {t for t, v in trend.items() if v[2]["trend_stage"] in UPTREND}
    after = dt.date.fromisoformat(last_done) if last_done and dt.date.fromisoformat(last_done) < bar_date \
        else bars[-2].date()
    after = max(after, bars[max(-11, -len(bars))].date())
    events = []
    for t, (_, _, info) in trend.items():
        for e in stage_events(info.pop("stage_series"), after):
            events.append({"ticker": t, **e})
    events.sort(key=lambda e: (e["date"], e["ticker"]))
    today_ev = [e for e in events if e["date"] == bar_date]
    wl_added = sorted({e["ticker"] for e in today_ev if e["to"] in UPTREND and e["from"] not in UPTREND})
    wl_removed = sorted({e["ticker"] for e in today_ev if e["from"] in UPTREND and e["to"] not in UPTREND})
    newly = {e["ticker"]: e["from"] for e in today_ev if e["ticker"] in wl_added}
    state.pop("stages", None)

    cands, status = [], {}
    first_fail = lambda rules: next((f"{r['rule']} {r['name']}" for r in _gates(rules) if r["ok"] is not True), "")
    for w in rows:
        t = w["ticker"]
        d = px[t]
        t_rules, s50, info = trend[t]
        w.update(score_map.get(t, {}))
        close = float(d["Close"].iloc[-1])
        base = find_base(d)
        st = {"close": round(close, 2), "rs_pct": pct126.get(t), **info,
              "vol50": float(d["Volume"].iloc[-51:-1].mean()),
              "pivot": base["pivot"] if base else float(d["High"].iloc[-60:].max()),
              "base_len": base["length"] if base else None, "excess63": w["excess63"]}
        status[t] = st
        if not w.get("passed"):
            st["stage"] = "no fundamentals on file yet (retried each run)"
            continue
        v_rules = valuation_rules(close, w, cfg)
        st.update({"pe": v_rules[0]["value"], "peg": v_rules[2]["value"], "pe_rel": v_rules[1]["value"]})
        if not _passes(_gates(t_rules), True):
            st["stage"] = "waiting: trend - " + (STAGE_LABEL.get(info["trend_stage"], "")
                                                 if info["trend_stage"] not in UPTREND else first_fail(t_rules))
            continue
        if v_rules[-1]["ok"] is False:
            st["stage"] = "waiting: valuation - " + first_fail(v_rules)
            continue
        setup = entry_setup(d, s50, cfg)
        if not setup:
            st["stage"] = f"{STAGE_LABEL[info['trend_stage']]}, waiting for breakout/pullback entry"
            continue
        st["stage"] = "SIGNAL: " + setup["setup"]
        cands.append({**w, "close": close, "setup": setup,
                      "proof": json.loads(w.get("rules") or "[]") + t_rules + v_rules})

    buys, missed = [], []
    if cands:
        cands.sort(key=lambda c: c["score"], reverse=True)

        held = set(open_pos.ticker) - {e["ticker"] for e in exits}
        sector_count = collections.Counter(universe[universe.ticker.isin(held)].sector)
        free = max(cfg.n_stocks - len(held), 0)
        slots = min(free, cfg.max_new_per_day)
        for c in cands:
            t = c["ticker"]
            why = None
            if t in held:
                continue
            cd = state["cooldowns"].get(t)
            if cd and int(((bars > pd.Timestamp(cd))).sum()) <= cfg.cooldown_days:
                why = f"11.4 cooldown after stop-out on {cd}"
            elif c.get("next_earnings") and not pd.isna(c["next_earnings"]) and \
                    0 <= (pd.Timestamp(c["next_earnings"]).date() - bar_date).days <= 7:
                why = f"6C results due {c['next_earnings']}"
            elif sector_count[c["sector"]] >= cfg.max_per_sector:
                why = f"7.5 already holding {cfg.max_per_sector} in {c['sector']}"
            elif len(buys) >= slots:
                why = "no free slot (11.1)"
            if not why:
                size = size_position(cfg, c["close"], c["setup"]["structure_low"])
                if size["skip_reason"]:
                    why = f"8.6 {size['skip_reason']}"
                else:
                    c["size"] = size
                    buys.append(c)
                    sector_count[c["sector"]] += 1
                    continue
            missed.append({"date": str(bar_date), "ticker": t, "score": round(c["score"], 3),
                           "setup": c["setup"]["setup"], "reason": why})

    append_csv(cfg.data_dir / "missed.csv", missed, ["date", "ticker"])
    skip_reason = {m["ticker"]: m["reason"] for m in missed}
    append_csv(cfg.data_dir / "setups.csv",
               [{"date": str(bar_date), "ticker": c["ticker"], "setup": c["setup"]["setup"],
                 "limit": round(c["setup"]["limit"], 4), "structure_low": round(c["setup"]["structure_low"], 4),
                 "result": "BUY" if c in buys else skip_reason.get(c["ticker"], "held")} for c in cands],
               ["date", "ticker"])
    entry = entry_status(cfg, bars, bar_date, status, buys, missed)
    append_csv(cfg.data_dir / "signals.csv",
               [{"date": str(bar_date), "ticker": b["ticker"], "setup": b["setup"]["setup"],
                 "close": round(b["close"], 2), "limit": round(b["setup"]["limit"], 2),
                 "stop": round(b["size"]["stop"], 2), "shares": b["size"]["shares"],
                 "score": round(b["score"], 3)} for b in buys] +
               [{"date": str(bar_date), "ticker": e["ticker"], "setup": "EXIT",
                 "close": round(e["close"], 2), "reason": e["reason"]} for e in exits],
               ["date", "ticker", "setup"])

    # ---------------- Report (section 12) ---------------- #
    mom = momentum_rows(trend, status, wl_map, score_map, sector_of)
    report = render_report(cfg, bar_date, fetched_at, src, buys, exits, stop_updates, held_notes,
                           wl_added, wl_removed, len(in_trend), len(universe), missed, stale_note, need_full,
                           int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0,
                           mom=mom, newly=newly, events=events)
    path = cfg.report_dir / f"{bar_date}.md"
    path.write_text(report)
    append_csv(cfg.data_dir / "runs.csv", [{
        "session": str(bar_date), "run_at": fetched_at, "universe": len(universe),
        "watchlist": len(in_trend), "buys": len(buys), "exits": len(exits),
        "stop_updates": len(stop_updates), "skipped_signals": len(missed),
        "missing_fundamentals": int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0,
        "watchlist_added": " ".join(wl_added[:50]),
        "watchlist_removed": " ".join(wl_removed[:50]),
        "notes": stale_note}], ["session"])
    peers = sector_peer_stats(wl, px)
    write_sheet_rows(cfg, bar_date, fetched_at, len(universe), mom, buys, exits, stop_updates,
                     missed, events, stale_note, status, entry,
                     int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0, peers, score_map, sector_of)
    state["last_bar_date"] = str(bar_date)
    save_state(cfg, state)
    log(report)
    return path


SHEET_HEADERS = {
    "Runs": ["Market", "Session", "Run at", "Universe", "Watchlist", "Buys", "Exits", "Stop updates",
             "Skipped signals", "Missing fundamentals", "Watchlist added", "Watchlist removed", "Notes", "Report"],
    "Signals": ["Market", "Session", "Ticker", "Sector", "Setup", "Setup detail", "Score", "Quality score",
                "Growth score", "Momentum score", "Last close", "Limit (max entry)", "Stop", "Disaster stop",
                "Shares", "Value", "ROCE/ROE avg", "Qtr sales YoY", "Qtr profit YoY", "Profit accel",
                "6m return pctile", "P/E", "PEG", "Unverified rules", "Manual checks", "Report"],
    "Position actions": ["Market", "Session", "Ticker", "Action", "Last close", "Entry", "P&L %", "Detail"],
    "Skipped signals": ["Market", "Session", "Ticker", "Setup", "Score", "Reason skipped"],
    "Changes": ["Market", "Session", "Ticker", "Sector", "Event", "From", "To", "Days in previous stage",
                "Score", "Last close", "Status today", "Screener.in", "Yahoo Finance"],
}
# New columns sit at the end of each tab so rows logged before they existed stay aligned.
SHEET_HEADERS["Signals"] += ["Screener.in", "Yahoo Finance", "Trend stage", "Days in stage", "Days in uptrend"]
# From v2 "Watchlist" in Runs = stocks in a confirmed/emerging uptrend; added/removed = entered/left that list.
SHEET_HEADERS["Runs"] += ["Confirmed", "Emerging", "Pullback in uptrend", "Stage changes"]
SHEET_HEADERS["Skipped signals"] += ["Trend stage", "Days in stage", "Last close"]
SHEET_HEADERS["Position actions"] += ["Trend stage", "Days in stage"]

# Tabs the sheet task clears and rewrites (current state), rather than appends to
BOARD_HEADERS = ["Ticker", "Sector", "Trend stage", "In stage since", "Days in stage", "In uptrend since",
                 "Days in uptrend", "Entry status", "Trigger to watch", "Status today", "Score", "Quality score",
                 "Growth score", "Momentum score", "50/200 cross", "6m return pctile", "3m vs index",
                 "% from 20DMA", "% from 50DMA", "% from 52w high", "Last close", "P/E", "PEG",
                 "Fund. rules passed", "Rules missed", "Next results", "Why it's great",
                 "vs competitors (sector peers in index)", "Next: what to check", "Screener.in", "Yahoo Finance",
                 "Session",
                 # numeric metrics (for the website and sorting); sector medians cover every index stock in the sector
                 "ROCE/ROE", "OCF/Profit", "Debt/Equity", "Qtr sales YoY", "Qtr profit YoY", "Profit accel",
                 "Sector peers", "Sector median ROCE/ROE", "Sector median sales YoY", "Sector median profit YoY",
                 "Sector median P/E", "Sector median Debt/Equity", "Rank in sector (ROCE/ROE)",
                 "Rank in sector (profit growth)", "Is financial"]
TODAY_HEADERS = ["Section", "Ticker", "Detail", "Trend stage", "Days in stage", "Score", "Last close", "Link"]
TODAY_BLOCK = {"india": ("A", "H"), "us": ("J", "Q")}
STAGE_ORDER = {"emerging": 0, "confirmed": 1, "pullback": 2}


def momentum_rows(trend, status, wl_map, score_map, sector_of):
    """Every stock in a confirmed or emerging uptrend, or pulling back inside one, best score first."""
    out = []
    for t, (_, _, info) in trend.items():
        if info["trend_stage"] not in STAGE_ORDER:
            continue
        w = dict(wl_map.get(t) or {})
        w.update({"ticker": t, "sector": w.get("sector") or sector_of.get(t, ""), **score_map.get(t, {})})
        out.append({"ticker": t, "stage": info["trend_stage"], "w": w, "st": status.get(t, {}),
                    "score": score_map.get(t, {}).get("score", 0.0)})
    out.sort(key=lambda r: -(r["score"] or 0))
    return out


def entry_status(cfg, bars, bar_date, status, buys, missed, lookback=5):
    """Per ticker: is there a buy setup today, or a recent one that is still buyable, or a trigger close by?"""
    out = {}
    for b in buys:
        out[b["ticker"]] = f"BUY today ({b['setup']['setup']}), limit {b['setup']['limit']:,.2f}"
    for m in missed:
        out.setdefault(m["ticker"], f"Setup today ({m['setup']}) but skipped: {m['reason']}")
    p = cfg.data_dir / "setups.csv"
    if p.exists():
        log = pd.read_csv(p, dtype={"ticker": str})
        log["date"] = pd.to_datetime(log["date"])
        recent = [d for d in bars[-1 - lookback:-1]]
        log = log[log["date"].isin(recent)].sort_values("date")
        for r in log.itertuples():
            if r.ticker in out or r.ticker not in status:
                continue
            ago = int((bars > r.date).sum())
            close = status[r.ticker].get("close")
            if close is None:
                continue
            if close < r.structure_low:
                v = "expired: broke below the setup low"
            elif close > r.limit:
                v = f"expired: now {close / r.limit - 1:+.1%} above its limit, don't chase"
            else:
                v = f"still under its limit {r.limit:,.2f}"
            out[r.ticker] = f"Setup {ago} session{'s' if ago > 1 else ''} ago ({r.setup}): {v}"
    for t, st in status.items():
        if t in out or st.get("trend_stage") not in IN_UPTREND:
            continue
        close, piv = st.get("close"), st.get("pivot")
        if st.get("trend_stage") == "pullback":
            out[t] = f"Below 50DMA: watch for a close back above {st['s50']:,.2f}"
        elif _ok(piv) and piv * 0.98 <= close < piv:
            out[t] = f"Near breakout: {close / piv - 1:.1%} below {piv:,.2f}"
        elif _ok(st.get("ext20")) and 0 <= st["ext20"] <= 0.02:
            out[t] = f"In 20DMA pullback zone ({st['s20']:,.2f})"
        elif _ok(st.get("ext50")) and 0 <= st["ext50"] <= 0.03:
            out[t] = f"In 50DMA pullback zone ({st['s50']:,.2f})"
        elif _ok(st.get("ext20")) and st["ext20"] > 0.15:
            out[t] = f"Extended: {st['ext20']:.0%} above 20DMA, wait for a pullback"
        else:
            out[t] = "Waiting"
    return out


def _board_numbers(w, pr):
    pr = pr or {}
    rk = lambda k: int(pr[k]) if _ok(pr.get(k)) else ""
    return [_num(w.get("quality_metric")), _num(w.get("ocf_np"), 2), _num(w.get("de"), 2),
            _num(w.get("q_sales_yoy")), _num(w.get("q_profit_yoy")), _num(w.get("accel")),
            pr.get("n", ""), _num(pr.get("med_q")), _num(pr.get("med_sales")), _num(pr.get("med_profit")),
            _num(pr.get("med_pe"), 1), _num(pr.get("med_de"), 2), rk("rank_q"), rk("rank_g"),
            "yes" if w.get("is_financial") in (True, "True") else ""]


def trigger_text(cfg, st):
    cur, parts = cfg.currency, []
    if _ok(st.get("pivot")):
        parts.append(f"Breakout > {cur}{st['pivot']:,.0f}" +
                     (f" on vol > {1.5 * st['vol50']:,.0f}" if _ok(st.get("vol50")) else ""))
    if _ok(st.get("s20")) and _ok(st.get("s50")):
        parts.append(f"pullback to {cur}{st['s20']:,.0f} (20DMA) / {cur}{st['s50']:,.0f} (50DMA)")
    return "; ".join(parts)


def stock_links(ticker: str) -> tuple[str, str]:
    """(Screener.in, Yahoo Finance) URLs for a ticker. Screener covers NSE stocks only, so it is blank for US."""
    from urllib.parse import quote
    t = str(ticker).strip()
    screener = ""
    if t.endswith((".NS", ".BO")):
        screener = f"https://www.screener.in/company/{quote(t.rsplit('.', 1)[0], safe='')}/"
    return screener, f"https://finance.yahoo.com/quote/{quote(t, safe='.-^')}/"


def _md_ticker(ticker: str) -> str:
    """Ticker as a markdown link: Screener for India, Yahoo for US."""
    screener, yahoo = stock_links(ticker)
    return f"[{ticker}]({screener or yahoo})"


def _num(v, nd=4):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return ""
        return round(float(v), nd)
    except (TypeError, ValueError):
        return str(v)


TREND_HINTS = {
    "4.1": "Not in a confirmed or emerging uptrend yet",
    "4.2": "Long-term (200-day) trend still pointing down: wait for it to turn up",
    "4.4": "Hasn't risen enough off its 1-year low yet",
    "4.5": "Too far below its 1-year high: needs to recover more first",
    "4.6": "Has lagged most stocks over 6 months: wait for it to start leading",
    "4.7": "Lagging the index over 3 months: wait for it to outperform",
}


def _ok(v):
    return v is not None and v != "" and not (isinstance(v, float) and math.isnan(v))


def sector_peer_stats(wl, px):
    """Per stock: how it compares with every other stock in the same sector of the index."""
    df = wl[wl.get("no_data") != True].copy() if "no_data" in wl else wl.copy()
    if not len(df):
        return {}
    def pe_now(r):
        d, eps = px.get(r.ticker), r.eps_ttm
        if d is None or not _ok(eps) or eps <= 0:
            return np.nan
        return float(d["Close"].iloc[-1]) / eps
    df["pe_now"] = [pe_now(r) for r in df.itertuples()]
    out = {}
    for sec, g in df.groupby("sector"):
        n = len(g)
        rank_q = g["quality_metric"].rank(ascending=False, method="min")
        rank_g = g["q_profit_yoy"].rank(ascending=False, method="min")
        med = g[["quality_metric", "q_sales_yoy", "q_profit_yoy", "pe_now", "de"]].median(numeric_only=True)
        for idx, r in g.iterrows():
            out[r.ticker] = {"sector": sec, "n": n, "rank_q": rank_q.get(idx), "rank_g": rank_g.get(idx),
                             "med_q": med.get("quality_metric"), "med_sales": med.get("q_sales_yoy"),
                             "med_profit": med.get("q_profit_yoy"), "med_pe": med.get("pe_now"),
                             "med_de": med.get("de"), "pe": r.pe_now}
    return out


def why_text(w, st):
    why = []
    q = w.get("quality_metric")
    label = "ROE" if w.get("is_financial") in (True, "True") else "ROCE"
    if _ok(q):
        why.append(f"Earns ~{q * 100:.0f} on every 100 of capital ({label} {q:.0%})")
    gs, gp = w.get("q_sales_yoy"), w.get("q_profit_yoy")
    if _ok(gs) and _ok(gp):
        why.append(f"Latest quarter: sales +{gs:.0%}, profit +{gp:.0%} vs a year ago")
    o, d = w.get("ocf_np"), w.get("de")
    if _ok(o) and o >= 1:
        why.append("Profits are backed by real cash")
    if _ok(d) and float(d) < 0.1:
        why.append("Almost no debt")
    peg = st.get("peg")
    if _ok(peg) and peg < 1:
        why.append(f"Cheap for its growth (PEG {peg:.1f})")
    return "\n".join("• " + x for x in why)


def peers_text(w, pr):
    if not pr or pr.get("n", 0) < 3:
        return "Too few sector peers in the index to compare"
    lab = "ROE" if w.get("is_financial") in (True, "True") else "ROCE"
    lines = []
    if _ok(w.get("quality_metric")) and _ok(pr.get("med_q")):
        lines.append(f"{lab} {w['quality_metric']:.0%} vs sector median {pr['med_q']:.0%} "
                     f"(#{int(pr['rank_q'])} of {pr['n']})")
    if _ok(w.get("q_profit_yoy")) and _ok(pr.get("med_profit")):
        lines.append(f"Profit growth {w['q_profit_yoy']:+.0%} vs sector {pr['med_profit']:+.0%} "
                     f"(#{int(pr['rank_g'])} of {pr['n']})")
    if _ok(w.get("q_sales_yoy")) and _ok(pr.get("med_sales")):
        lines.append(f"Sales growth {w['q_sales_yoy']:+.0%} vs sector {pr['med_sales']:+.0%}")
    if _ok(pr.get("pe")) and _ok(pr.get("med_pe")):
        tag = "premium" if pr["pe"] > pr["med_pe"] * 1.1 else ("discount" if pr["pe"] < pr["med_pe"] * 0.9 else "in line")
        lines.append(f"P/E {pr['pe']:.0f} vs sector {pr['med_pe']:.0f} ({tag})")
    if _ok(w.get("de")) and _ok(pr.get("med_de")) and w.get("is_financial") not in (True, "True"):
        lines.append(f"Debt/equity {float(w['de']):.2f} vs sector {pr['med_de']:.2f}")
    return f"vs {pr['n'] - 1} {pr['sector']} peers in the index:\n" + "\n".join("• " + x for x in lines)


def next_text(cfg, w, st, bar_date):
    cur, nxt = cfg.currency, []
    stage = st.get("stage", "")
    s50, s200, piv, vol = st.get("s50"), st.get("s200"), st.get("pivot"), st.get("vol50")
    vol_need = f"{1.5 * vol:,.0f}" if _ok(vol) else "1.5x average"
    if stage.startswith("SIGNAL"):
        nxt.append("Buy setup triggered: see the Signals tab for limit, stop and size")
    elif "waiting for breakout/pullback" in stage:
        s20 = st.get("s20")
        nxt.append(f"Breakout trigger: close above {cur}{piv:,.0f} on volume > {vol_need} shares")
        if _ok(s20):
            nxt.append(f"Or pullback trigger: dip to ~{cur}{s20:,.0f} (20-day avg) or ~{cur}{s50:,.0f} (50-day avg), "
                       "then close above the prior day's high")
    elif stage.startswith("waiting: valuation"):
        nxt.append("Trend is fine but valuation fails: wait for the price or the P/E to cool")
    elif stage.startswith("waiting: trend"):
        ts = st.get("trend_stage")
        if ts == "pullback" and _ok(s50):
            nxt.append(f"Uptrend intact, price dipped below the 50-day avg: trigger is a close back above {cur}{s50:,.0f}")
        elif ts == "stalling":
            nxt.append("50-day avg has flattened: wait for it to turn up again")
        elif ts in UPTREND:
            nxt.append("In an uptrend but lagging other stocks or the index: wait for it to start leading")
        elif _ok(s50) and _ok(s200):
            extra = (f", and the 50-day avg must rise to within 3% of the 200-day ({cur}{s200:,.0f})"
                     if s50 < s200 else "")
            nxt.append(f"Trend trigger: close above {cur}{max(s50, s200):,.0f} with the 50-day avg "
                       f"({cur}{s50:,.0f}) turning up{extra}")
    in_trend_wait = "waiting for breakout/pullback" in stage
    if (stage.startswith("SIGNAL") or in_trend_wait) and _ok(st.get("close")):
        nxt.append(f"Risk plan if entered near {cur}{st['close']:,.0f}: stop no lower than {cur}{st['close'] * 0.92:,.0f} "
                   f"(-8%); at +15% ({cur}{st['close'] * 1.15:,.0f}) move stop to entry, then trail the 50-day avg")
    elif stage.startswith("waiting"):
        nxt.append("Not buyable yet: no entry until the trigger above is met")
    peg, eps, g = st.get("peg"), w.get("eps_ttm"), w.get("profit_cagr_3y")
    if _ok(peg) and peg > 2 and _ok(eps) and _ok(g) and g > 0:
        nxt.append(f"Pricey: PEG {peg:.1f}. Price for PEG 2 is ~{cur}{2 * g * 100 * eps:,.0f}")
    elif _ok(st.get("pe_rel")) and st["pe_rel"] > 1.3:
        nxt.append(f"P/E is {st['pe_rel']:.1f}x its own 5-year median: prefer a dip")
    ne = w.get("next_earnings")
    try:
        ned = pd.Timestamp(ne).date() if _ok(ne) else None
    except Exception:
        ned = None
    gp, gs = w.get("q_profit_yoy"), w.get("q_sales_yoy")
    if ned and 0 <= (ned - bar_date).days <= 30:
        last = f" (last: sales {gs:+.0%}, profit {gp:+.0%})" if _ok(gp) and _ok(gs) else ""
        nxt.append(f"Results {ned:%d %b}: want sales growth >= 15% and profit growth >= 20% YoY{last}; "
                   "a 2nd straight slowdown = skip/exit")
    o, d = w.get("ocf_np"), w.get("de")
    if _ok(o) and o < 0.9:
        nxt.append(f"Cash conversion only {o:.2f}x of profit: check receivables and inventory")
    if _ok(d) and float(d) > 0.3:
        nxt.append(f"Debt/equity {float(d):.2f}: check that it isn't rising")
    if cfg.market == "india":
        nxt.append("Screener: promoter pledge < 5%, promoter holding not down > 2 pts in 4 qtrs; NSE: not in ASM/GSM")
    return "\n".join("• " + x for x in nxt)


def write_sheet_rows(cfg, bar_date, fetched_at, n_universe, mom, buys, exits, stop_updates, missed,
                     events, notes, status, entry, n_no_data, peers=None, score_map=None, sector_of=None):
    """Sheet rows for this session, saved as data/<market>/sheet_rows/<session>.json.

    "rows":    append-only tabs (one row per event).
    "replace": current-state tabs the sheet task clears and rewrites: this market's Board tab and its block of Today.
    """
    import os
    m, sess = cfg.market.upper(), str(bar_date)
    repo = os.environ.get("GITHUB_REPOSITORY")
    link = f"https://github.com/{repo}/blob/main/reports/{cfg.market}/{sess}.md" if repo else ""
    score_map, sector_of = score_map or {}, sector_of or {}
    sc = lambda t: score_map.get(t, {}).get("score")
    stg = lambda t: STAGE_LABEL.get(status.get(t, {}).get("trend_stage", ""), "")
    dis = lambda t: status.get(t, {}).get("days_in_stage") or ""
    pct = lambda v: _num(v, 4) if _ok(v) else ""
    one_link = lambda t: (stock_links(t)[0] or stock_links(t)[1])
    rows = {k: [] for k in SHEET_HEADERS}
    count = lambda k: sum(1 for r in mom if r["stage"] == k)
    rows["Runs"].append([m, sess, fetched_at, n_universe, count("confirmed") + count("emerging"), len(buys),
                         len(exits), len(stop_updates), len(missed), n_no_data,
                         " ".join(e["ticker"] for e in events if e["date"] == bar_date and e["from"] not in IN_UPTREND)[:400],
                         " ".join(e["ticker"] for e in events if e["date"] == bar_date and e["to"] not in IN_UPTREND)[:400],
                         notes, link, count("confirmed"), count("emerging"), count("pullback"), len(events)])
    for b in buys:
        s_, st = b["size"], status.get(b["ticker"], {})
        rows["Signals"].append([
            m, sess, b["ticker"], b["sector"], b["setup"]["setup"], b["setup"]["detail"], _num(b["score"], 3),
            _num(b["score_quality"], 3), _num(b["score_growth"], 3), _num(b["score_momentum"], 3),
            _num(b["close"], 2), _num(b["setup"]["limit"], 2), _num(s_["stop"], 2),
            _num(b["close"] * (1 - cfg.disaster_stop_pct), 2), s_["shares"], _num(s_["value"], 0),
            _num(b.get("quality_metric")), _num(b.get("q_sales_yoy")), _num(b.get("q_profit_yoy")),
            _num(b.get("accel")), _num(st.get("rs_pct"), 3), _num(st.get("pe"), 1), _num(st.get("peg"), 2),
            b.get("unverified") or "", "; ".join(cfg.manual_checks), link, *stock_links(b["ticker"]),
            stg(b["ticker"]), dis(b["ticker"]), st.get("days_up") or ""])
    for e in exits:
        rows["Position actions"].append([m, sess, e["ticker"], "EXIT", _num(e["close"], 2), _num(e["entry"], 2),
                                         _num(e["pnl_pct"] * 100, 2), e["reason"], stg(e["ticker"]), dis(e["ticker"])])
    for u in stop_updates:
        rows["Position actions"].append([m, sess, u["ticker"], "STOP UPDATE", "", "", "",
                                         f"{u['old']:.2f} -> {u['new']:.2f}: {u['why']}", stg(u["ticker"]),
                                         dis(u["ticker"])])
    for x in missed:
        rows["Skipped signals"].append([m, sess, x["ticker"], x["setup"], x["score"], x["reason"], stg(x["ticker"]),
                                        dis(x["ticker"]), _num(status.get(x["ticker"], {}).get("close"), 2)])

    # Changes: stage changes (all sessions since the last run), plus today's signals and position actions
    for e in events:
        t, st = e["ticker"], status.get(e["ticker"], {})
        rows["Changes"].append([m, str(e["date"]), t, sector_of.get(t, ""), event_label(e["from"], e["to"]),
                                STAGE_LABEL[e["from"]], STAGE_LABEL[e["to"]], e["days_prev"], _num(sc(t), 3),
                                _num(st.get("close"), 2), st.get("stage", ""), *stock_links(t)])
    for b in buys:
        t = b["ticker"]
        rows["Changes"].append([m, sess, t, b["sector"], f"BUY signal ({b['setup']['setup']})", "", "", "",
                                _num(b["score"], 3), _num(b["close"], 2),
                                f"limit {b['setup']['limit']:,.2f}, stop {b['size']['stop']:,.2f}", *stock_links(t)])
    for x in missed:
        t = x["ticker"]
        rows["Changes"].append([m, sess, t, sector_of.get(t, ""), f"Setup skipped ({x['setup']})", "", "", "",
                                x["score"], _num(status.get(t, {}).get("close"), 2), x["reason"], *stock_links(t)])
    for e in exits:
        t = e["ticker"]
        rows["Changes"].append([m, sess, t, sector_of.get(t, ""), "EXIT", "", "", "", _num(sc(t), 3),
                                _num(e["close"], 2), e["reason"], *stock_links(t)])
    for u in stop_updates:
        t = u["ticker"]
        rows["Changes"].append([m, sess, t, sector_of.get(t, ""), "STOP MOVED", "", "", "", _num(sc(t), 3), "",
                                f"{u['old']:.2f} -> {u['new']:.2f}", *stock_links(t)])

    # Board: every stock in an uptrend or pullback; most recent stage change first, then score
    board = []
    top_ids = {r["ticker"] for r in sorted([r for r in mom if r["w"].get("passed")],
                                         key=lambda r: -(r["score"] or 0))[:cfg.watchlist_tab_top]}
    for r in sorted(mom, key=lambda r: (-(pd.Timestamp(r["st"].get("stage_since") or "1900-01-01").value),
                                         -(r["score"] or 0))):
        w, st, t = r["w"], r["st"], r["ticker"]
        ne = w.get("next_earnings")
        board.append([
            t, w.get("sector", ""), STAGE_LABEL[r["stage"]], str(st.get("stage_since") or ""),
            st.get("days_in_stage") or "", str(st.get("up_since") or ""), st.get("days_up") or "",
            entry.get(t, ""), trigger_text(cfg, st), st.get("stage", ""), _num(w.get("score"), 3),
            _num(w.get("score_quality"), 3), _num(w.get("score_growth"), 3), _num(w.get("score_momentum"), 3),
            st.get("cross_txt", ""), _num(st.get("rs_pct"), 3), pct(st.get("excess63")), pct(st.get("ext20")),
            pct(st.get("ext50")), pct(st.get("from_hi52") - 1 if _ok(st.get("from_hi52")) else None),
            _num(st.get("close"), 2), _num(st.get("pe"), 1), _num(st.get("peg"), 2),
            f"{int(w['fund_ok'])}/{int(w['fund_known'])}" if _ok(w.get("fund_known")) and w.get("fund_known") else "",
            w.get("fund_misses") if _ok(w.get("fund_misses")) else "",
            str(ne) if _ok(ne) else "",
            why_text(w, st) if t in top_ids else "",
            peers_text(w, (peers or {}).get(t)) if t in top_ids else "",
            next_text(cfg, w, st, bar_date) if t in top_ids else "", *stock_links(t), sess,
            *_board_numbers(w, (peers or {}).get(t))])

    # Today: this market's block - actions, then changes, then stocks close to a trigger
    tl = [[f"{m}: session {sess}", "", f"run {fetched_at}. In uptrend: {count('confirmed')} confirmed, "
           f"{count('emerging')} emerging, {count('pullback')} pulling back. "
           f"{len(events)} stage changes since the last run.", "", "", "", "", link]]
    for b in buys:
        t = b["ticker"]
        tl.append(["Act: BUY", t, f"{b['setup']['setup']}: limit {b['setup']['limit']:,.2f}, stop "
                   f"{b['size']['stop']:,.2f}, {b['size']['shares']} shares. Skip if it opens above the limit.",
                   stg(t), dis(t), _num(b["score"], 3), _num(b["close"], 2), one_link(t)])
    for e in exits:
        t = e["ticker"]
        tl.append(["Act: EXIT", t, f"{e['reason']} (P&L {e['pnl_pct']:+.1%})", stg(t), dis(t), _num(sc(t), 3),
                   _num(e["close"], 2), one_link(t)])
    for u in stop_updates:
        t = u["ticker"]
        tl.append(["Act: MOVE STOP", t, f"{u['old']:.2f} -> {u['new']:.2f}: {u['why']}", stg(t), dis(t),
                   _num(sc(t), 3), "", one_link(t)])
    if not (buys or exits or stop_updates):
        tl.append(["Act", "", "No action today", "", "", "", "", ""])
    for x in missed:
        t = x["ticker"]
        tl.append(["Setup skipped", t, f"{x['setup']}: {x['reason']}", stg(t), dis(t), x["score"],
                   _num(status.get(t, {}).get("close"), 2), one_link(t)])
    for e in sorted(events, key=lambda e: (e["date"] != bar_date, -(sc(e["ticker"]) or 0)))[:25]:
        t = e["ticker"]
        when = "" if e["date"] == bar_date else f" (on {e['date']})"
        tl.append(["Changed", t, event_label(e["from"], e["to"]) + when, stg(t), dis(t), _num(sc(t), 3),
                   _num(status.get(t, {}).get("close"), 2), one_link(t)])
    if len(events) > 25:
        tl.append(["Changed", "", f"+{len(events) - 25} more stage changes in the Changes tab", "", "", "", "", ""])
    near = [r for r in mom if str(entry.get(r["ticker"], "")).startswith(("Near breakout", "In 20DMA", "In 50DMA",
                                                                          "Setup "))]
    for r in near[:10]:
        t = r["ticker"]
        tl.append(["Near a trigger", t, entry[t], stg(t), dis(t), _num(r["score"], 3),
                   _num(r["st"].get("close"), 2), one_link(t)])

    c1, c2 = TODAY_BLOCK[cfg.market]
    board_tab = f"Board - {'India' if cfg.market == 'india' else 'US'}"
    replace = {
        board_tab: {"clear": f"'{board_tab}'!A1:AZ5000", "start": f"'{board_tab}'!A1",
                    "values": [BOARD_HEADERS] + board},
        "Today": {"clear": f"'Today'!{c1}1:{c2}300", "start": f"'Today'!{c1}1",
                  "values": [TODAY_HEADERS] + tl},
    }
    out = cfg.data_dir / "sheet_rows"
    out.mkdir(exist_ok=True)
    (out / f"{sess}.json").write_text(json.dumps({"headers": SHEET_HEADERS, "rows": rows, "replace": replace},
                                                 default=str, indent=1))


def render_report(cfg, bar_date, fetched_at, src, buys, exits, stop_updates, held_notes,
                  wl_added, wl_removed, n_watch, n_universe, missed, stale_note, rebuilt, n_no_data=0,
                  mom=None, newly=None, events=None):
    cur = cfg.currency
    mom = mom or []
    L = [f"# {cfg.market.upper()} Quality Momentum - signals from {bar_date} close", "",
         f"Data: {src.name}, fetched {fetched_at}. Universe {n_universe} stocks; "
         f"{n_watch} in a confirmed or emerging uptrend (fundamentals {'refreshed today' if rebuilt else 'cached'})."
         + (f" Fundamentals still missing for {n_no_data} stocks; they are retried each run." if n_no_data else ""),
         ""]
    if stale_note:
        L += [f"> {stale_note}", ""]
    if not (buys or exits or stop_updates):
        L += ["**No action today.**", ""]

    if buys:
        L += ["## BUY (place before next open)", "",
              "| Ticker | Setup | Score | Last close | Limit (max) | Stop | Disaster GTT | Shares | Value |",
              "|---|---|---|---|---|---|---|---|---|"]
        for b in buys:
            s = b["size"]
            L.append(f"| {_md_ticker(b['ticker'])} | {b['setup']['setup']} | {b['score']:.2f} | {cur}{b['close']:,.2f} | "
                     f"{cur}{b['setup']['limit']:,.2f} | {cur}{s['stop']:,.2f} | "
                     f"{cur}{b['close'] * (1 - cfg.disaster_stop_pct):,.2f} | {s['shares']} | {cur}{s['value']:,.0f} |")
        L.append("")
        L.append("Skip a buy if the stock opens above its limit (11.6). After buying, add a row to "
                 f"`data/{cfg.market}/positions.csv` with your actual fill price and the stop.")
        L.append("")
        for b in buys:
            L += [f"### Proof: {b['ticker']} ({b['sector']})", "", f"Setup: {b['setup']['detail']}.", "",
                  "| Rule | Check | Value | Threshold | Result |", "|---|---|---|---|---|"]
            for r in b["proof"]:
                pct = r["rule"] in PCT_RULES
                val, thr = _fmt(r["value"], pct), _fmt(r["threshold"], pct)
                if r["rule"] == "2.1" and r["value"]:
                    val = f"{cur}{r['value'] / cfg.mcap_div:,.0f}{cfg.mcap_label}"
                    thr = f"{cur}{r['threshold'] / cfg.mcap_div:,.0f}{cfg.mcap_label}"
                res = {True: "pass", False: "FAIL", None: "unverified"}[r["ok"]]
                if r.get("gate") is False or r["rule"][0] in "23":
                    res = {True: "pass", False: "miss", None: "unverified"}[r["ok"]] + " (scored)"
                L.append(f"| {r['rule']} | {r['name']} | {val} | {thr} | {res} |")
            if cfg.manual_checks:
                link = cfg.research_url.format(symbol=b["ticker"].split(".")[0]) if cfg.research_url else ""
                L.append("")
                L.append("Check manually before buying: " + "; ".join(cfg.manual_checks) +
                         (f" ([research page]({link}))" if link else "") + ".")
            L.append("")

    if exits:
        L += ["## EXIT (sell at next open)", "", "| Ticker | Last close | Entry | P&L | Rule |", "|---|---|---|---|---|"]
        for e in exits:
            L.append(f"| {_md_ticker(e['ticker'])} | {cur}{e['close']:,.2f} | {cur}{e['entry']:,.2f} | "
                     f"{e['pnl_pct']:+.1%} | {e['reason']} |")
        L += ["", "Remove the row from positions.csv after selling.", ""]

    if stop_updates:
        L += ["## STOP UPDATES", ""]
        L += [f"- {s['ticker']}: {cur}{s['old']:,.2f} -> {cur}{s['new']:,.2f} ({s['why']}). "
              "positions.csv already updated; move your GTT too." for s in stop_updates]
        L.append("")

    if held_notes:
        L += ["## Open positions", ""] + [f"- {h}" for h in held_notes] + [""]

    if mom:
        cnt = {k: sum(1 for r in mom if r["stage"] == k) for k in STAGE_ORDER}
        L += ["## MOMENTUM", "",
              f"Confirmed uptrend {cnt['confirmed']}, emerging {cnt['emerging']}, pullback in uptrend "
              f"{cnt['pullback']}. Ranked by score (quality 40%, growth 30%, momentum 30%). Full list: Momentum tab.",
              "", "| Ticker | Stage | Days in stage | Score | 50/200 cross | 6m RS pctile | % from 20DMA | Fund. rules missed | Status |",
              "|---|---|---|---|---|---|---|---|---|"]
        for stg in ("emerging", "confirmed"):
            for r in [x for x in mom if x["stage"] == stg][:12]:
                st, w = r["st"], r["w"]
                rs = f"{st['rs_pct']:.0%}" if _ok(st.get("rs_pct")) else "n/a"
                ext = f"{st['ext20']:+.1%}" if _ok(st.get("ext20")) else ""
                miss = w.get("fund_misses") if _ok(w.get("fund_misses")) else ""
                L.append(f"| {_md_ticker(r['ticker'])} | {STAGE_LABEL[stg]} | {st.get('days_in_stage') or ''} | {r['score']:.2f} | "
                         f"{st.get('cross_txt', '')} | {rs} | {ext} | {miss} | {st.get('stage', '')} |")
        L.append("")
    if events:
        L += [f"## STAGE CHANGES SINCE THE LAST RUN ({len(events)})", "",
              "| Date | Ticker | Change | Days in previous stage |", "|---|---|---|---|"]
        for e in events[-60:]:
            L.append(f"| {e['date']} | {_md_ticker(e['ticker'])} | {event_label(e['from'], e['to'])} | {e['days_prev']} |")
        L.append("")
    if wl_added or wl_removed:
        L += ["## UPTREND CHANGES SINCE LAST SESSION", ""]
        if wl_added:
            L.append(f"- Entered ({len(wl_added)}): " + ", ".join(
                f"{_md_ticker(t)}" + (f" (was {STAGE_LABEL.get(newly.get(t), '?').lower()})" if newly and newly.get(t) else "")
                for t in wl_added[:30]) + (" ..." if len(wl_added) > 30 else ""))
        if wl_removed:
            L.append(f"- Left ({len(wl_removed)}): {', '.join(wl_removed[:30])}" + (" ..." if len(wl_removed) > 30 else ""))
        L.append("")

    if missed:
        L += ["## Signals without a slot (logged to missed.csv)", ""]
        L += [f"- {m['ticker']} ({m['setup']}, score {m['score']}): {m['reason']}" for m in missed]
        L.append("")
    return "\n".join(L)


def cli(cfg: Config):
    ap = argparse.ArgumentParser(description=f"{cfg.market.upper()} Quality Momentum daily scan")
    ap.add_argument("--rebuild", action="store_true", help="force a full watchlist rebuild (Layer A)")
    ap.add_argument("--force", action="store_true", help="re-run even if this session was already processed")
    ap.add_argument("--capital", type=float, help="override capital C")
    ap.add_argument("--n", type=int, help="override number of stocks N")
    ap.add_argument("--hours", help="run only in these hours, e.g. 7-11 (weekdays only); otherwise exit")
    ap.add_argument("--hours-tz", default=None, help="time zone for --hours (default: the market's)")
    ap.add_argument("--pdf", action="store_true", help="skip the scan (the workflow then only builds/emails the PDF)")
    a = ap.parse_args()
    if a.pdf:
        return
    if a.hours:
        lo, hi = (int(x) for x in a.hours.split("-"))
        now = dt.datetime.now(ZoneInfo(a.hours_tz or cfg.tz))
        if now.weekday() > 4 or not lo <= now.hour <= hi:
            return
    if a.capital:
        cfg.capital = a.capital
    if a.n:
        cfg.n_stocks = a.n
    path = run(cfg, rebuild=a.rebuild, force=a.force)
    if path:
        print(f"\nReport saved to {path}")
