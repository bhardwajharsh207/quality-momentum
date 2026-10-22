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
    cooldown_days: int = 20

    # Costs (8.6): estimated round-trip cost = cost_fixed + cost_pct * value
    cost_fixed: float = 0.0
    cost_pct: float = 0.0
    max_cost_pct: float = 0.01

    # Quality gate (2.x)
    roce_avg_min: float = 0.13
    roce_each_min: float = 0.11
    roe_fin_min: float = 0.14
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
    require_fresh_cross: bool = True  # 4.3: golden cross within last 65 sessions (False = any uptrend age)

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


def _rule(rule, name, value, threshold, ok):
    if isinstance(value, (np.floating, np.integer)):
        value = float(value)
    return {"rule": rule, "name": name, "value": value, "threshold": threshold,
            "ok": None if ok is None else bool(ok)}


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
        "passed": _passes(rules, cfg.strict_missing), "unverified": ",".join(_unverified(rules)),
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


def trend_rules(d: pd.DataFrame, rs_pct: float | None, ret63: float, bench63: float, cfg: Config):
    c, lo, hi = d["Close"], d["Low"], d["High"]
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    above = s50 > s200
    cross = (above & ~above.shift(1, fill_value=True)).iloc[-65:].any()
    lo52, hi52 = lo.iloc[-252:].min(), hi.iloc[-252:].max()
    rules = [
        _rule("4.1", "Close > 50DMA > 200DMA", f"{c.iloc[-1]:.2f} / {s50.iloc[-1]:.2f} / {s200.iloc[-1]:.2f}",
              "ordered", bool(c.iloc[-1] > s50.iloc[-1] > s200.iloc[-1])),
        _rule("4.2", "200DMA rising (vs 20d ago)", f"{s200.iloc[-1]:.2f} vs {s200.iloc[-21]:.2f}",
              "higher", bool(s200.iloc[-1] > s200.iloc[-21])),
        *([_rule("4.3", "50/200 golden cross in last 65d", bool(cross), True, bool(cross))]
          if cfg.require_fresh_cross else []),
        _rule("4.4", "Close / 52w low", c.iloc[-1] / lo52, 1.30, bool(c.iloc[-1] >= 1.30 * lo52)),
        _rule("4.5", "Close / 52w high", c.iloc[-1] / hi52, 0.75, bool(c.iloc[-1] >= 0.75 * hi52)),
        _rule("4.6", "6m return percentile", rs_pct, 1 - cfg.rs_top_pct,
              None if rs_pct is None else rs_pct >= 1 - cfg.rs_top_pct),
        _rule("4.7", "3m return vs benchmark", f"{ret63:.1%} vs {bench63:.1%}", "higher", bool(ret63 > bench63)),
    ]
    return rules, s50


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
    # 6B pullback (last 5 sessions before today)
    w = d.iloc[-6:-1]
    near = (w["Low"] <= s50.iloc[-6:-1] * 1.03).any()
    dry = w["Volume"].mean() < vol50
    bounce = c.iloc[-1] > d["High"].iloc[-2]
    if near and dry and bounce:
        return {"setup": "pullback", "structure_low": float(w["Low"].min()),
                "limit": c.iloc[-1] * (1 + cfg.max_chase_pct),
                "detail": f"touched 50DMA zone, pullback vol {w['Volume'].mean() / vol50:.2f}x avg, closed above prior high"}
    return None


# --------------------------------------------------------------------------- #
# Ranking and sizing (sections 7, 8)
# --------------------------------------------------------------------------- #


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    pr = lambda s, asc=True: s.rank(pct=True, ascending=asc).fillna(0.5)
    q = (pr(df["quality_metric"]) + pr(df["ocf_np"]) + pr(df["de"], asc=False)) / 3
    g = (pr(df["q_sales_yoy"]) + pr(df["q_profit_yoy"]) + pr(df["accel"])) / 3
    m = (pr(df["ret126"]) + pr(df["excess63"])) / 2
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
        raise RuntimeError(f"Fetched data ends {bar_date}, older than the last processed session {last_done}. "
                           "Yahoo returned stale data; the next attempt will retry.")

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
    old_pass = set(old_wl[old_wl.passed == True].ticker) if len(old_wl) else set()
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
            ne = pd.to_datetime(wl["next_earnings"], errors="coerce").dt.date
            fa = pd.to_datetime(wl["fetched_at"], errors="coerce").dt.date
            due = list(wl.ticker[(ne <= bar_date) & (fa <= ne)])
        if "no_data" in wl:
            due += list(wl.ticker[wl.no_data == True])[:cfg.refetch_cap]
        due = sorted(set(due))
        if due:
            log(f"[Layer A] refreshing {len(due)} stocks (new results or missing data) ...")
            fresh = build_watchlist(cfg, src, universe, px, only=due, log=log)
            keep_old = wl[wl.ticker.isin(fresh.ticker[fresh.no_data == True]) & (wl.get("no_data") != True)]
            fresh = fresh[~fresh.ticker.isin(keep_old.ticker)]
            wl = pd.concat([wl[~wl.ticker.isin(fresh.ticker)], fresh], ignore_index=True)
    wl.to_csv(wl_path, index=False)
    passed = wl[wl.passed == True].copy()
    passed.drop(columns=["rules"], errors="ignore").to_csv(cfg.data_dir / "watchlist.csv", index=False)
    new_pass = set(passed.ticker)
    wl_added, wl_removed = sorted(new_pass - old_pass), sorted(old_pass - new_pass) if old_pass else []

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

    # ---------------- Layer B: signals ---------------- #
    ret = lambda d, n: d["Close"].iloc[-1] / d["Close"].iloc[-1 - n] - 1 if len(d) > n else np.nan
    uni_ret126 = pd.Series({t: ret(px[t], 126) for t in universe.ticker if t in px}).dropna()
    pct126 = uni_ret126.rank(pct=True)
    bench63 = ret(bench, 63)

    cands, evals, status = [], [], {}
    first_fail = lambda rules: next((f"{r['rule']} {r['name']}" for r in rules if r["ok"] is not True), "")
    for w in passed.to_dict("records"):
        t = w["ticker"]
        d = px.get(t)
        if d is None or len(d) < 260:
            status[t] = {"stage": "no price data"}
            continue
        r126, r63 = ret(d, 126), ret(d, 63)
        t_rules, s50 = trend_rules(d, pct126.get(t), r63, bench63, cfg)
        w.update({"ret126": r126, "excess63": r63 - bench63})
        evals.append(w)
        close = float(d["Close"].iloc[-1])
        v_rules = valuation_rules(close, w, cfg)
        s200 = d["Close"].rolling(200).mean()
        base = find_base(d)
        status[t] = {"close": round(close, 2), "rs_pct": pct126.get(t), "pe": v_rules[0]["value"],
                     "peg": v_rules[2]["value"], "pe_rel": v_rules[1]["value"],
                     "s50": float(s50.iloc[-1]), "s200": float(s200.iloc[-1]), "s200_20": float(s200.iloc[-21]),
                     "vol50": float(d["Volume"].iloc[-51:-1].mean()),
                     "pivot": base["pivot"] if base else float(d["High"].iloc[-60:].max()),
                     "base_len": base["length"] if base else None}
        if not _passes(t_rules, True):
            status[t]["stage"] = "waiting: trend - " + first_fail(t_rules)
            continue
        if v_rules[-1]["ok"] is False:
            status[t]["stage"] = "waiting: valuation - " + first_fail(v_rules)
            continue
        setup = entry_setup(d, s50, cfg)
        if not setup:
            status[t]["stage"] = "in trend, waiting for breakout/pullback entry"
            continue
        status[t]["stage"] = "SIGNAL: " + setup["setup"]
        cands.append({**w, "close": close, "setup": setup,
                      "proof": json.loads(w.get("rules") or "[]") + t_rules + v_rules})

    buys, missed = [], []
    if cands:
        scored = add_scores(pd.DataFrame(evals))
        score_map = scored.set_index("ticker")[["score", "score_quality", "score_growth", "score_momentum"]]
        for c in cands:
            c.update(score_map.loc[c["ticker"]].to_dict())
        cands.sort(key=lambda c: c["score"], reverse=True)

        held = set(open_pos.ticker) - {e["ticker"] for e in exits}
        held_sectors = set(universe[universe.ticker.isin(held)].sector)
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
            elif c["sector"] in held_sectors:
                why = f"7.5 already holding {c['sector']}"
            elif len(buys) >= slots:
                why = "no free slot (11.1)"
            if not why:
                size = size_position(cfg, c["close"], c["setup"]["structure_low"])
                if size["skip_reason"]:
                    why = f"8.6 {size['skip_reason']}"
                else:
                    c["size"] = size
                    buys.append(c)
                    held_sectors.add(c["sector"])
                    continue
            missed.append({"date": str(bar_date), "ticker": t, "score": round(c["score"], 3),
                           "setup": c["setup"]["setup"], "reason": why})

    append_csv(cfg.data_dir / "missed.csv", missed, ["date", "ticker"])
    append_csv(cfg.data_dir / "signals.csv",
               [{"date": str(bar_date), "ticker": b["ticker"], "setup": b["setup"]["setup"],
                 "close": round(b["close"], 2), "limit": round(b["setup"]["limit"], 2),
                 "stop": round(b["size"]["stop"], 2), "shares": b["size"]["shares"],
                 "score": round(b["score"], 3)} for b in buys] +
               [{"date": str(bar_date), "ticker": e["ticker"], "setup": "EXIT",
                 "close": round(e["close"], 2), "reason": e["reason"]} for e in exits],
               ["date", "ticker", "setup"])

    # ---------------- Report (section 12) ---------------- #
    report = render_report(cfg, bar_date, fetched_at, src, buys, exits, stop_updates, held_notes,
                           wl_added, wl_removed, len(passed), len(universe), missed, stale_note, need_full,
                           int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0)
    path = cfg.report_dir / f"{bar_date}.md"
    path.write_text(report)
    append_csv(cfg.data_dir / "runs.csv", [{
        "session": str(bar_date), "run_at": fetched_at, "universe": len(universe),
        "watchlist": len(passed), "buys": len(buys), "exits": len(exits),
        "stop_updates": len(stop_updates), "skipped_signals": len(missed),
        "missing_fundamentals": int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0,
        "watchlist_added": " ".join(wl_added[:50]) if old_pass else "",
        "watchlist_removed": " ".join(wl_removed[:50]),
        "notes": stale_note}], ["session"])
    peers = sector_peer_stats(wl, px)
    write_sheet_rows(cfg, bar_date, fetched_at, len(universe), passed, buys, exits, stop_updates,
                     missed, wl_added if old_pass else [], wl_removed, stale_note, status,
                     int((wl.get("no_data") == True).sum()) if "no_data" in wl else 0, peers)
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
    "Watchlist": ["Market", "Session", "Ticker", "Sector", "Status today", "Why it's great",
                  "vs competitors (sector peers in index)", "Next: what to check",
                  "Last close", "ROCE/ROE avg",
                  "OCF/Profit", "Debt/Equity", "Qtr sales YoY", "Qtr profit YoY", "Profit accel",
                  "6m return pctile", "P/E", "PEG", "Next results", "Unverified rules",
                  "Screener.in", "Yahoo Finance"],
}
# Link columns sit at the end of each tab so rows logged before they existed stay aligned.
SHEET_HEADERS["Signals"] += ["Screener.in", "Yahoo Finance"]


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
    "4.1": "Price is below its 50/200-day averages: wait for it to climb back above both",
    "4.2": "Long-term (200-day) trend still pointing down: wait for it to turn up",
    "4.3": "Uptrend isn't a fresh one: wait for a new base or a clean pullback",
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
    elif stage.startswith("in trend"):
        nxt.append(f"Breakout trigger: close above {cur}{piv:,.0f} on volume > {vol_need} shares")
        nxt.append(f"Or pullback trigger: dip to ~{cur}{s50:,.0f} (50-day avg), then close above the prior day's high")
    elif stage.startswith("waiting: valuation"):
        nxt.append("Trend is fine but valuation fails: wait for the price or the P/E to cool")
    else:
        rule = stage.split(" - ")[-1].split(" ")[0] if " - " in stage else ""
        if rule == "4.1" and _ok(s50) and _ok(s200):
            nxt.append(f"Trend trigger: close above {cur}{max(s50, s200):,.0f} with the 50-day avg ({cur}{s50:,.0f}) "
                       f"above the 200-day ({cur}{s200:,.0f})")
        elif rule == "4.2" and _ok(s200):
            nxt.append(f"Trend trigger: 200-day avg ({cur}{s200:,.0f}) must start rising "
                       f"(it was {cur}{st.get('s200_20', 0):,.0f} 20 days ago)")
        elif rule == "4.3":
            nxt.append(f"Trend is old, not fresh: wait for a new 6-20 week base; breakout level ~{cur}{piv:,.0f}")
        elif rule in ("4.4", "4.5", "4.6", "4.7"):
            nxt.append("Momentum is weak vs other stocks: wait for it to start leading")
    if (stage.startswith("SIGNAL") or stage.startswith("in trend")) and _ok(st.get("close")):
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


def write_sheet_rows(cfg, bar_date, fetched_at, n_universe, passed, buys, exits, stop_updates, missed,
                     wl_added, wl_removed, notes, status, n_no_data, peers=None):
    """Rows to append to the Google Sheet, one list per tab, saved as data/<market>/sheet_rows/<session>.json."""
    import os
    m, sess = cfg.market.upper(), str(bar_date)
    repo = os.environ.get("GITHUB_REPOSITORY")
    link = f"https://github.com/{repo}/blob/main/reports/{cfg.market}/{sess}.md" if repo else ""
    rows = {k: [] for k in SHEET_HEADERS}
    rows["Runs"].append([m, sess, fetched_at, n_universe, len(passed), len(buys), len(exits), len(stop_updates),
                         len(missed), n_no_data, " ".join(wl_added[:60]), " ".join(wl_removed[:60]), notes, link])
    for b in buys:
        s, st = b["size"], status.get(b["ticker"], {})
        rows["Signals"].append([
            m, sess, b["ticker"], b["sector"], b["setup"]["setup"], b["setup"]["detail"], _num(b["score"], 3),
            _num(b["score_quality"], 3), _num(b["score_growth"], 3), _num(b["score_momentum"], 3),
            _num(b["close"], 2), _num(b["setup"]["limit"], 2), _num(s["stop"], 2),
            _num(b["close"] * (1 - cfg.disaster_stop_pct), 2), s["shares"], _num(s["value"], 0),
            _num(b.get("quality_metric")), _num(b.get("q_sales_yoy")), _num(b.get("q_profit_yoy")),
            _num(b.get("accel")), _num(st.get("rs_pct"), 3), _num(st.get("pe"), 1), _num(st.get("peg"), 2),
            b.get("unverified") or "", "; ".join(cfg.manual_checks), link, *stock_links(b["ticker"])])
    for e in exits:
        rows["Position actions"].append([m, sess, e["ticker"], "EXIT", _num(e["close"], 2), _num(e["entry"], 2),
                                         _num(e["pnl_pct"] * 100, 2), e["reason"]])
    for u in stop_updates:
        rows["Position actions"].append([m, sess, u["ticker"], "STOP UPDATE", "", "", "",
                                         f"{u['old']:.2f} -> {u['new']:.2f}: {u['why']}"])
    for x in missed:
        rows["Skipped signals"].append([m, sess, x["ticker"], x["setup"], x["score"], x["reason"]])
    for w in passed.sort_values("ticker").to_dict("records"):
        st = status.get(w["ticker"], {})
        rows["Watchlist"].append([
            m, sess, w["ticker"], w.get("sector", ""), st.get("stage", ""), why_text(w, st),
            peers_text(w, (peers or {}).get(w["ticker"])), next_text(cfg, w, st, bar_date),
            _num(st.get("close"), 2),
            _num(w.get("quality_metric")), _num(w.get("ocf_np"), 2), _num(w.get("de"), 2),
            _num(w.get("q_sales_yoy")), _num(w.get("q_profit_yoy")), _num(w.get("accel")),
            _num(st.get("rs_pct"), 3), _num(st.get("pe"), 1), _num(st.get("peg"), 2),
            str(w.get("next_earnings") or "") if not pd.isna(w.get("next_earnings")) else "",
            w.get("unverified") or "", *stock_links(w["ticker"])])
    out = cfg.data_dir / "sheet_rows"
    out.mkdir(exist_ok=True)
    (out / f"{sess}.json").write_text(json.dumps({"headers": SHEET_HEADERS, "rows": rows}, default=str, indent=1))


def render_report(cfg, bar_date, fetched_at, src, buys, exits, stop_updates, held_notes,
                  wl_added, wl_removed, n_watch, n_universe, missed, stale_note, rebuilt, n_no_data=0):
    cur = cfg.currency
    L = [f"# {cfg.market.upper()} Quality Momentum - signals from {bar_date} close", "",
         f"Data: {src.name}, fetched {fetched_at}. Universe {n_universe} stocks; "
         f"watchlist {n_watch} ({'rebuilt today' if rebuilt else 'cached'})."
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

    if wl_added or wl_removed:
        L += ["## WATCHLIST CHANGES", ""]
        if wl_added:
            L.append(f"- Added ({len(wl_added)}): {', '.join(_md_ticker(t) for t in wl_added[:30])}" + (" ..." if len(wl_added) > 30 else ""))
        if wl_removed:
            L.append(f"- Removed ({len(wl_removed)}): {', '.join(wl_removed[:30])}" + (" ..." if len(wl_removed) > 30 else ""))
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
    a = ap.parse_args()
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
