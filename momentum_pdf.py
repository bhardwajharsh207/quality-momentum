"""
Momentum PDF: stocks in new and ongoing bullish momentum, one chart each as proof.

    python momentum_pdf.py              # both markets, top 10 per section
    python momentum_pdf.py --top 15

Reads the latest scan output (data/<market>/sheet_rows/<session>.json, Board values) to pick the stocks,
downloads ~15 months of prices from Yahoo, and writes reports/momentum/<date>.pdf.

Sections per market:
  New momentum      - Emerging, or the 50 DMA crossed above the 200 DMA within the last 30 sessions
  Ongoing momentum  - Confirmed uptrend with an older golden cross
Each section: top N by score (quality 40% + growth 30% + momentum 30%).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

import qm_core as qc

BASE = Path(__file__).resolve().parent
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
C20, C50, C200, CROSS = "#eb6834", "#2a78d6", "#6250d6", "#1baf7a"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})


def load_board(market):
    files = sorted((BASE / "data" / market / "sheet_rows").glob("*.json"))
    for f in reversed(files):
        d = json.loads(f.read_text())
        rep = d.get("replace") or {}
        key = next((k for k in rep if k.startswith("Board")), None)
        if key:
            vals = rep[key]["values"]
            return f.stem, pd.DataFrame(vals[1:], columns=vals[0])
    raise SystemExit(f"No Board data for {market}; run the scan first.")


def pick(board, top):
    b = board.copy()
    b["Score"] = pd.to_numeric(b["Score"], errors="coerce")
    b["Days in uptrend"] = pd.to_numeric(b["Days in uptrend"], errors="coerce")
    age = b["50/200 cross"].str.extract(r"crossed (\d+) sessions", expand=False).astype(float)
    fresh = age <= 30
    new = b[(b["Trend stage"] == "Emerging") | ((b["Trend stage"] == "Confirmed uptrend") & fresh)]
    ongoing = b[(b["Trend stage"] == "Confirmed uptrend") & ~fresh]
    return (new.sort_values("Score", ascending=False).head(top),
            ongoing.sort_values("Score", ascending=False).head(top))


def fmt_pct(v, signed=True):
    try:
        return f"{float(v):+.1%}" if signed else f"{float(v):.0%}"
    except (TypeError, ValueError):
        return "n/a"


def chart(ax, axv, d, row, cur):
    c = d["Close"]
    s20, s50, s200 = (c.rolling(n).mean() for n in (20, 50, 200))
    w = d.index >= d.index[-1] - pd.Timedelta(days=270)
    x = d.index[w]
    ax.plot(x, c[w], color=INK, lw=1.2, label="Price")
    for s, col, lab in [(s20, C20, "20 DMA"), (s50, C50, "50 DMA"), (s200, C200, "200 DMA")]:
        ax.plot(x, s[w], color=col, lw=1.6, label=lab)
    hi52 = d["High"].iloc[-252:].max()
    ax.axhline(hi52, color=MUTED, lw=0.8, ls=(0, (4, 3)))
    ax.text(x[len(x) // 2], hi52, f"52w high {cur}{hi52:,.0f}", va="bottom", ha="center", color=INK2, fontsize=7)
    above = (s50 > s200)
    cross = above & ~above.shift(1, fill_value=False) & s200.shift(1).notna()
    cd = [t for t in cross[cross].index if t >= x[0]]
    if cd:
        t = cd[-1]
        ax.axvline(t, color=CROSS, lw=1, ls=":")
        ax.annotate(f"Golden cross\n{t:%d %b}", (t, float(s50.loc[t])), xytext=(6, -26),
                    textcoords="offset points", color=INK2, fontsize=7,
                    arrowprops=dict(arrowstyle="-", color=CROSS, lw=0.8))
    ax.annotate(f"{cur}{c.iloc[-1]:,.0f}", (x[-1], c.iloc[-1]), xytext=(4, 0), textcoords="offset points",
                va="center", fontsize=7.5, color=INK, fontweight="bold")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=4, frameon=False, fontsize=7, handlelength=1.6,
              borderaxespad=0.2)
    ax.set_xlim(x[0], x[-1] + pd.Timedelta(days=12))
    v = d["Volume"][w]
    axv.bar(x, v, width=1.0, color="#c3c2b7")
    axv.plot(x, d["Volume"].rolling(50).mean()[w], color=INK2, lw=1)
    axv.set_yticks([])
    axv.spines[["top", "right", "left"]].set_visible(False)
    axv.text(x[0], v.max(), " Volume (line = 50-day avg)", va="top", fontsize=6.5, color=INK2)
    axv.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.tick_params(labelbottom=False)


def stats_text(row, cur):
    lines = [
        ("Stage", f"{row['Trend stage']} - {row['Days in stage']} sessions (in uptrend {row['Days in uptrend']})"),
        ("50/200", row["50/200 cross"]),
        ("Score", f"{float(row['Score']):.2f}  (Q {float(row['Quality score']):.2f} / G {float(row['Growth score']):.2f}"
                  f" / M {float(row['Momentum score']):.2f})" if row["Score"] not in ("", None) else "n/a"),
        ("6m RS", f"{fmt_pct(row['6m return pctile'], False)} percentile;  3m vs index {fmt_pct(row['3m vs index'])}"),
        ("Position", f"{fmt_pct(row['% from 20DMA'])} vs 20 DMA, {fmt_pct(row['% from 50DMA'])} vs 50 DMA, "
                     f"{fmt_pct(row['% from 52w high'])} from 52w high"),
        ("Valuation", f"P/E {row['P/E'] or 'n/a'}, PEG {row['PEG'] or 'n/a'}"),
        ("Fundamentals", f"{row['Fund. rules passed'] or 'n/a'} rules pass" +
                         (f"; misses {row['Rules missed']}" if row["Rules missed"] else "")),
        ("Entry status", row["Entry status"]),
        ("Watch", row["Trigger to watch"]),
        ("Now", row["Status today"]),
    ]
    return lines


def stock_block(fig, rect, d, row, cur):
    l, b, w, h = rect
    ax = fig.add_axes([l, b + h * 0.30, w * 0.57, h * 0.56])
    axv = fig.add_axes([l, b + h * 0.08, w * 0.57, h * 0.20], sharex=ax)
    chart(ax, axv, d, row, cur)
    fig.text(l, b + h * 0.995, f"{row['Ticker']}", fontsize=12, fontweight="bold", color=INK, va="top")
    fig.text(l, b + h * 0.945, f"{row['Sector']}", fontsize=8, color=INK2, va="top")
    y = b + h * 0.90
    for k, v in stats_text(row, cur):
        wrapped = textwrap.wrap(str(v or ""), 38) or [""]
        fig.text(l + w * 0.61, y, k, fontsize=7, color=INK2, va="top", fontweight="bold")
        fig.text(l + w * 0.725, y, "\n".join(wrapped[:3]), fontsize=7, color=INK, va="top", linespacing=1.3)
        y -= 0.0125 * max(1, min(3, len(wrapped))) + 0.006


def cover(pdf, sections, sessions):
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.07, 0.94, "Bullish momentum: new and ongoing", fontsize=18, fontweight="bold", color=INK)
    fig.text(0.07, 0.915, "Quality Momentum scanner  |  " +
             ", ".join(f"{m.upper()} close {s}" for m, s in sessions.items()), fontsize=9, color=INK2)
    method = [
        "New momentum: stage Emerging (50 DMA just crossed or is within 3% of the 200 DMA, price above both, 50 DMA "
        "rising), or a golden cross (50 DMA above 200 DMA) within the last 30 sessions.",
        "Ongoing momentum: Confirmed uptrend (price > 50 DMA > 200 DMA, both rising) with an older golden cross.",
        "Ranked by score = 40% quality + 30% growth + 30% momentum, each a percentile within the index. Top names shown.",
        "Each page: 9-month chart with 20/50/200 DMA, the latest golden cross, 52-week high and volume, plus the numbers.",
        "A stock here is not a buy signal by itself: buys need an entry setup (breakout or pullback) and valuation OK. "
        "See 'Entry status'. Not investment advice.",
    ]
    y = 0.88
    for m in method:
        wr = textwrap.wrap(m, 110)
        fig.text(0.07, y, "\n".join(wr), fontsize=8, color=INK, va="top", linespacing=1.35)
        y -= 0.013 * len(wr) + 0.008
    y -= 0.01
    for (title, df) in sections:
        fig.text(0.07, y, f"{title}  ({len(df)})", fontsize=10, fontweight="bold", color=INK, va="top")
        y -= 0.018
        hdr = ["Ticker", "Stage", "In stage", "Score", "6m RS", "vs 20DMA", "Entry status"]
        xs = [0.07, 0.20, 0.36, 0.42, 0.49, 0.56, 0.65]
        for xx, hh in zip(xs, hdr):
            fig.text(xx, y, hh, fontsize=7, color=INK2, fontweight="bold", va="top")
        y -= 0.013
        for _, r in df.iterrows():
            vals = [r["Ticker"], r["Trend stage"], r["Days in stage"], f"{float(r['Score']):.2f}",
                    fmt_pct(r["6m return pctile"], False), fmt_pct(r["% from 20DMA"]),
                    textwrap.shorten(str(r["Entry status"]), 62)]
            for xx, vv in zip(xs, vals):
                fig.text(xx, y, str(vv), fontsize=7, color=INK, va="top")
            y -= 0.0122
        y -= 0.012
        if y < 0.06:
            pdf.savefig(fig)
            plt.close(fig)
            fig = plt.figure(figsize=(8.27, 11.69))
            y = 0.94
    pdf.savefig(fig)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--markets", default="india,us")
    a = ap.parse_args()
    src = qc.YahooSource()
    sections, sessions, pages = [], {}, []
    for m in a.markets.split(","):
        cur = "₹" if m == "india" else "$"
        sess, board = load_board(m)
        sessions[m] = sess
        new, ongoing = pick(board, a.top)
        tickers = list(new["Ticker"]) + list(ongoing["Ticker"])
        px = src.prices(tickers, period="2y")
        label = "India" if m == "india" else "US"
        for title, df in [(f"{label}: New momentum", new), (f"{label}: Ongoing momentum", ongoing)]:
            df = df[df["Ticker"].isin(px)]
            sections.append((title, df))
            pages.append((title, df, px, cur))
    out = BASE / "reports" / "momentum"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{max(sessions.values())}.pdf"
    with PdfPages(path) as pdf:
        cover(pdf, sections, sessions)
        for title, df, px, cur in pages:
            rows = [r for _, r in df.iterrows()]
            for i in range(0, len(rows), 2):
                fig = plt.figure(figsize=(8.27, 11.69))
                fig.text(0.07, 0.965, title, fontsize=9, color=INK2, fontweight="bold")
                for j, row in enumerate(rows[i:i + 2]):
                    stock_block(fig, (0.07, 0.50 - j * 0.47, 0.88, 0.44), px[row["Ticker"]], row, cur)
                pdf.savefig(fig)
                plt.close(fig)
        info = pdf.infodict()
        info["Title"] = "Bullish momentum: new and ongoing"
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
