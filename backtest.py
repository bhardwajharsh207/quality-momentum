"""
Backtest of the Quality Momentum rules over the past N months.

    python backtest.py india --months 12
    python backtest.py us --months 12

What it replays, day by day, with only data available on that day:
  - trend template (4.1-4.7), entry setups (6A breakout, 6B pullback), gap check (11.6),
  - stops and exits (9.1, 9.2, 9.3, 9.5), cooldown (11.4), 2 new entries/day and N slots (11.1).
What it cannot replay point-in-time (Yahoo has no history of fundamentals):
  - the quality and earnings gates (2.x, 3.x), valuation (5.x), results-date filter (6C), fundamental exit (9.4).
So it reports the technical engine on the whole universe (no look-ahead) and, separately, on today's
watchlist (look-ahead: those stocks are known to be good today). Universe = today's index members
(survivorship bias, flattering both).

Outputs in backtest/<market>/: summary.md, trades.csv, layers.csv, equity.csv
"""
from __future__ import annotations

import argparse
import importlib
import math
from pathlib import Path

import numpy as np
import pandas as pd

import qm_core as qc


def load_cfg(market):
    return importlib.import_module(f"{market}_scan").CONFIG


def frames(px, tickers):
    cols = {k: pd.DataFrame({t: px[t][k] for t in tickers if t in px})
            for k in ["Open", "High", "Low", "Close", "Volume"]}
    idx = cols["Close"].index.sort_values()
    return {k: v.reindex(idx) for k, v in cols.items()}


VARIANTS = {
    "current": "Rules as live today",
    "no_fresh": "Fix 1: drop 4.3 (uptrend no longer has to be new)",
    "dip_ok": "Fix 1 + Fix 2: drop 4.3, and allow a dip below the 50DMA while above the 200DMA",
}


def trend_mask(F, bench_close, cfg, variant="current"):
    c, h, l = F["Close"], F["High"], F["Low"]
    s50, s200 = c.rolling(50, min_periods=50).mean(), c.rolling(200, min_periods=200).mean()
    above = (s50 > s200).astype(float).where(s200.notna())
    cross = (above.diff() == 1)
    cross65 = cross.astype(float).rolling(65, min_periods=1).max() > 0
    lo52 = l.rolling(252, min_periods=200).min()
    hi52 = h.rolling(252, min_periods=200).max()
    r126 = c / c.shift(126) - 1
    pct = r126.rank(axis=1, pct=True)
    r63 = c / c.shift(63) - 1
    b63 = (bench_close / bench_close.shift(63) - 1).reindex(c.index).ffill()
    hist_ok = c.notna().cumsum() >= 260
    if variant in ("no_fresh", "dip_ok"):
        cross65 = pd.DataFrame(True, index=c.index, columns=c.columns)
    price_ok = (c > s200) if variant == "dip_ok" else (c > s50)
    m = (price_ok & (s50 > s200) & (s200 > s200.shift(20)) & cross65 &
         (c >= 1.30 * lo52) & (c >= 0.75 * hi52) & (pct >= 1 - cfg.rs_top_pct) &
         r63.gt(b63, axis=0) & hist_ok)
    return m.fillna(False), s50, pct


def simulate_trade(d: pd.DataFrame, s50: pd.Series, i: int, limit: float, structure_low: float, cfg):
    """d = one stock's OHLCV (calendar-aligned, may contain NaN rows); signal on bar i, entry at i+1 open."""
    n = len(d)
    if i + 1 >= n or pd.isna(d["Open"].iloc[i + 1]):
        return None
    entry = float(d["Open"].iloc[i + 1])
    if entry > limit:
        return {"status": "skipped", "reason": "11.6 opened above limit", "entry": entry}
    stop = max(structure_low, entry * (1 - cfg.max_stop_pct))
    if stop >= entry:
        stop = entry * (1 - cfg.max_stop_pct)
    init_stop, be = stop, False
    for j in range(i + 1, n):
        cl = d["Close"].iloc[j]
        if pd.isna(cl):
            continue
        held = j - (i + 1)
        reason = None
        if cl <= stop:
            reason = "9.1 breakeven stop" if be else "9.1 stop"
        elif be and d.index[j].weekday() == 4 and cl < s50.iloc[j]:
            reason = "9.3 weekly close < 50DMA"
        elif held >= 40 and cl < entry * 1.05:
            reason = "9.5 time stop"
        if reason:
            if j + 1 < n and not pd.isna(d["Open"].iloc[j + 1]):
                px_out, out_i = float(d["Open"].iloc[j + 1]), j + 1
            else:
                px_out, out_i = float(cl), j
            return {"status": "closed", "reason": reason, "entry": entry, "stop": init_stop,
                    "exit": px_out, "exit_i": out_i, "entry_i": i + 1}
        if not be and cl >= entry * 1.15:
            stop, be = entry, True
    last = d["Close"].dropna()
    return {"status": "open", "reason": "still open", "entry": entry, "stop": init_stop,
            "exit": float(last.iloc[-1]), "exit_i": n - 1, "entry_i": i + 1}


def md_table(df):
    cols = list(df.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["india", "us"])
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--variant", choices=list(VARIANTS), default="current")
    a = ap.parse_args()
    cfg = load_cfg(a.market)
    out = Path(__file__).resolve().parent / "backtest" / a.market
    if a.variant != "current":
        out = out / a.variant
    out.mkdir(parents=True, exist_ok=True)

    uni_path = cfg.data_dir / "universe.csv"
    universe = pd.read_csv(uni_path, dtype=str) if uni_path.exists() else cfg.universe_loader()
    wl_path = cfg.data_dir / "watchlist.csv"
    watch = set(pd.read_csv(wl_path).ticker) if wl_path.exists() else set()
    sector = dict(zip(universe.ticker, universe.sector))

    src = qc.YahooSource()
    tickers = list(universe.ticker)
    print(f"Downloading 4y prices for {len(tickers)} stocks ...")
    px = src.prices(tickers + [cfg.benchmark], period="4y")
    bench = px[cfg.benchmark]["Close"]
    F = frames(px, tickers)
    c, o = F["Close"], F["Open"]
    cal = c.index
    test_start = len(cal) - a.months * 21
    test_dates = cal[test_start:]
    print(f"Test window {test_dates[0].date()} to {test_dates[-1].date()} ({len(test_dates)} sessions)")

    trend, s50_all, mom_pct = trend_mask(F, bench, cfg, a.variant)

    # ---- signals and per-signal trades (each stock: one open trade at a time, cooldown after stop) ----
    trades = []
    busy_until, cooldown_until = {}, {}
    for i in range(test_start, len(cal)):
        day = cal[i]
        for t in trend.columns[trend.iloc[i].values]:
            if busy_until.get(t, -1) >= i or cooldown_until.get(t, -1) >= i:
                continue
            d = pd.DataFrame({k: F[k][t] for k in F}).iloc[:i + 1].dropna()
            if len(d) < 260 or d.index[-1] != day:
                continue
            setup = qc.entry_setup(d, d["Close"].rolling(50).mean(), cfg)
            if not setup:
                continue
            full = pd.DataFrame({k: F[k][t] for k in F})
            res = simulate_trade(full, s50_all[t], i, setup["limit"], setup["structure_low"], cfg)
            if not res:
                continue
            row = {"signal_date": day.date(), "ticker": t, "sector": sector.get(t, ""),
                   "setup": setup["setup"], "detail": setup["detail"], "on_watchlist_today": t in watch,
                   "momentum_rank": round(float(mom_pct.iloc[i][t]), 3),
                   **{k: res.get(k) for k in ["status", "reason", "entry", "stop", "exit"]}}
            if res["status"] != "skipped":
                ei, xi = res["entry_i"], res["exit_i"]
                row.update({"entry_date": cal[ei].date(), "exit_date": cal[xi].date(), "sessions": xi - ei,
                            "return_pct": round((res["exit"] / res["entry"] - 1) * 100 - cfg.cost_pct * 100, 2)})
                b0, b1 = bench.reindex(cal).ffill().iloc[ei], bench.reindex(cal).ffill().iloc[xi]
                row["bench_return_pct"] = round((b1 / b0 - 1) * 100, 2)
                for h in (20, 60):
                    if ei + h < len(cal) and not pd.isna(c[t].iloc[ei + h]):
                        row[f"fwd{h}_pct"] = round((c[t].iloc[ei + h] / res["entry"] - 1) * 100, 2)
                busy_until[t] = xi
                if res["reason"] == "9.1 stop":
                    cooldown_until[t] = xi + cfg.cooldown_days
            trades.append(row)
        if (i - test_start) % 21 == 0:
            print(f"  {day.date()}: {len(trades)} signals so far")
    T = pd.DataFrame(trades)
    T.to_csv(out / "trades.csv", index=False)

    # ---- layer attribution: 60-session forward return from next open ----
    fwd60 = c.shift(-60) / o.shift(-1) - 1
    bfwd60 = (bench.reindex(cal).ffill().shift(-60) / bench.reindex(cal).ffill() - 1)
    window = slice(test_start, len(cal) - 61)
    excess_all = fwd60.sub(bfwd60, axis=0)
    def layer(mask, name):
        f, e = fwd60.iloc[window], excess_all.iloc[window]
        if mask is not None:
            f, e = f.where(mask.iloc[window]), e.where(mask.iloc[window])
        vals, ex = f.stack().dropna(), e.stack().dropna()
        return {"layer": name, "observations": int(len(vals)),
                "avg_60d_return_pct": round(vals.mean() * 100, 2) if len(vals) else None,
                "median_60d_return_pct": round(vals.median() * 100, 2) if len(vals) else None,
                "pct_beating_benchmark": round((ex > 0).mean() * 100, 1) if len(ex) else None}
    sig_mask = pd.DataFrame(False, index=cal, columns=c.columns)
    if len(T):
        for r in T[T.status != "skipped"].itertuples():
            sig_mask.loc[pd.Timestamp(r.signal_date), r.ticker] = True
    wl_cols = [t for t in c.columns if t in watch]
    wl_mask = pd.DataFrame(False, index=cal, columns=c.columns)
    wl_mask[wl_cols] = True
    L = pd.DataFrame([layer(None, "Every stock, every day"),
                      layer(trend, "Passes trend template (4.x)"),
                      layer(sig_mask, "Trend + entry setup = signal (no fundamentals)"),
                      layer(sig_mask & wl_mask, "Signal on today's watchlist (look-ahead)")])
    L.to_csv(out / "layers.csv", index=False)

    # ---- portfolio simulation: N slots, max 2 new per day, equal weight, ranked by momentum ----
    eq, cash_slots, open_pos, curve = 1.0, cfg.n_stocks, [], []
    taken = T[T.status != "skipped"].copy() if len(T) else T
    if len(taken):
        taken["entry_date"] = pd.to_datetime(taken["entry_date"])
        taken["exit_date"] = pd.to_datetime(taken["exit_date"])
    port_trades = []
    for day in test_dates:
        # close positions exiting today
        still = []
        for p in open_pos:
            if p["exit_date"] <= day:
                ret = p["exit"] / p["entry"] - 1 - cfg.cost_pct
                eq += p["alloc"] * ret
                cash_slots += 1
                port_trades.append(p | {"port_return_pct": round(ret * 100, 2)})
            else:
                still.append(p)
        open_pos = still
        if len(taken):
            todays = taken[taken.entry_date == day].sort_values("momentum_rank", ascending=False)
            held_sectors = {p["sector"] for p in open_pos}
            new = 0
            for r in todays.to_dict("records"):
                if cash_slots == 0 or new >= cfg.max_new_per_day:
                    break
                if r["sector"] in held_sectors:
                    continue
                r["alloc"] = eq / cfg.n_stocks
                open_pos.append(r)
                held_sectors.add(r["sector"])
                cash_slots -= 1
                new += 1
        mtm = eq
        for p in open_pos:
            px_now = c[p["ticker"]].loc[:day].dropna()
            if len(px_now):
                mtm += p["alloc"] * (px_now.iloc[-1] / p["entry"] - 1)
        curve.append({"date": day.date(), "strategy": mtm})
    E = pd.DataFrame(curve)
    bser = bench.reindex(test_dates).ffill()
    E["benchmark"] = (bser / bser.iloc[0]).values
    E.to_csv(out / "equity.csv", index=False)
    P = pd.DataFrame(port_trades)

    # ---- summary ----
    def stats(df, col="return_pct"):
        if df is None or not len(df):
            return {}
        r = df[col]
        w, l = r[r > 0], r[r <= 0]
        return {"trades": len(r), "win_rate": f"{len(w) / len(r):.0%}",
                "avg_win": f"{w.mean():.1f}%" if len(w) else "n/a",
                "avg_loss": f"{l.mean():.1f}%" if len(l) else "n/a",
                "payoff": f"{w.mean() / abs(l.mean()):.2f}" if len(w) and len(l) and l.mean() else "n/a",
                "avg_trade": f"{r.mean():.2f}%"}
    closed = taken[taken.status == "closed"] if len(taken) else taken
    strat_ret = E["strategy"].iloc[-1] - 1
    bench_ret = E["benchmark"].iloc[-1] - 1
    dd = (E["strategy"] / E["strategy"].cummax() - 1).min()
    lines = [f"# Backtest: {a.market.upper()}, {test_dates[0].date()} to {test_dates[-1].date()}", "",
             f"Variant: **{a.variant}** - {VARIANTS[a.variant]}", "",
             f"Universe {len(c.columns)} stocks (today's members). Today's watchlist: {len(watch)} stocks.", "",
             "## Does each layer pick better stocks? (60-session forward return from next open)", "",
             md_table(L), "",
             "## Every signal as its own trade (exit rules applied, % costs deducted)", ""]
    for name, df in [("All signals", closed),
                     ("Signals on today's watchlist (look-ahead)",
                      closed[closed.on_watchlist_today] if len(closed) else closed)]:
        s = stats(df)
        lines.append(f"- **{name}:** " + (", ".join(f"{k} {v}" for k, v in s.items()) if s else "none"))
    if len(T):
        lines.append(f"- Signals skipped by the gap rule (11.6): {int((T.status == 'skipped').sum())}")
        lines.append(f"- Exit reasons: " + ", ".join(f"{k} {v}" for k, v in closed.reason.value_counts().items()))
    lines += ["", f"## Portfolio: {cfg.n_stocks} slots, max {cfg.max_new_per_day} new/day, equal weight", "",
              f"- Strategy {strat_ret:+.1%} vs benchmark {bench_ret:+.1%}; max drawdown {dd:.1%}; "
              f"{len(P)} closed trades, {len(open_pos)} still open.",
              "- No fundamental gate in this simulation, so it understates what the full rules aim for.", "",
              "## Caveats", "",
              "- Fundamentals (2.x, 3.x, 5.x) cannot be replayed: Yahoo keeps no history of them.",
              "- Universe is today's index members: stocks that were dropped are missing (flatters results).",
              "- Fixed costs (e.g. India's ₹16 DP charge) are not deducted; percentage costs are."]
    (out / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
