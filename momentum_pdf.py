"""
Momentum PDF: stocks in new and ongoing bullish momentum, with a 1-year chart each as proof.

    python momentum_pdf.py              # both markets, top 5 per section
    python momentum_pdf.py --top 8

Reads the latest scan output (data/<market>/sheet_rows/<session>.json) to pick the stocks, downloads prices
from Yahoo, and writes reports/momentum/<date>.pdf plus a short text summary (<date>.txt) used for the email.

Sections per market (top N by score = 40% quality + 30% growth + 30% momentum):
  New momentum      - Emerging, or the 50-day avg crossed above the 200-day avg within the last 30 sessions
  Ongoing momentum  - Confirmed uptrend that started earlier
"""
from __future__ import annotations

import argparse
import json
import re
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

import qm_core as qc
import importlib

BASE = Path(__file__).resolve().parent
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
C50, C200, UP = "#2a78d6", "#6250d6", "#1baf7a"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.edgecolor": GRID,
                     "xtick.color": INK2, "ytick.color": INK2, "text.parse_math": False})

RULE_WORDS = {"2.1": "size", "2.2": "return on capital", "2.2b": "return on assets",
              "2.3": "steady return on capital", "2.3/2.10": "steady return on capital", "2.4": "3-yr sales growth",
              "2.5": "3-yr profit growth", "2.6": "debt", "2.7": "cash conversion", "3.1": "quarterly sales growth",
              "3.2": "quarterly profit growth", "3.3": "profit growth slowing", "3.4": "quarterly EBITDA growth"}


# --------------------------------------------------------------------------- data
def load_latest(market):
    for f in sorted((BASE / "data" / market / "sheet_rows").glob("*.json"), reverse=True):
        d = json.loads(f.read_text())
        rep = d.get("replace") or {}
        key = next((k for k in rep if k.startswith("Board")), None)
        if key:
            vals = rep[key]["values"]
            sig = {r[2]: r for r in d["rows"].get("Signals", [])}
            return f.stem, pd.DataFrame(vals[1:], columns=vals[0]), sig
    raise SystemExit(f"No Board data for {market}; run the scan first.")


def load_fund(market):
    p = BASE / "data" / market / "watchlist_full.csv"
    return pd.read_csv(p).set_index("ticker") if p.exists() else pd.DataFrame()


def pick(board, top):
    b = board.copy()
    b["Score"] = pd.to_numeric(b["Score"], errors="coerce")
    age = b["50/200 cross"].str.extract(r"crossed (\d+) sessions", expand=False).astype(float)
    fresh = age <= 30
    new = b[(b["Trend stage"] == "Emerging") | ((b["Trend stage"] == "Confirmed uptrend") & fresh)]
    ongoing = b[(b["Trend stage"] == "Confirmed uptrend") & ~fresh]
    return (new.sort_values("Score", ascending=False).head(top),
            ongoing.sort_values("Score", ascending=False).head(top))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(cur, v):
    return f"{cur}{v:,.0f}" if v is not None else "n/a"


# --------------------------------------------------------------------------- plain-language lines
def trend_line(r):
    days, up_days = r["Days in stage"], r["Days in uptrend"]
    up_since = pd.to_datetime(r["In uptrend since"], errors="coerce")
    parts = [f"{r['Trend stage']} for {days} trading days"]
    if not pd.isna(up_since):
        parts[0] += f"; uptrend since {up_since:%d %b %Y} ({up_days} days)"
    e50, hi, ix = num(r["% from 50DMA"]), num(r["% from 52w high"]), num(r["3m vs index"])
    if e50 is not None:
        parts.append(f"price {abs(e50):.0%} {'above' if e50 >= 0 else 'below'} its 50-day avg")
    if hi is not None:
        parts.append("at its 1-year high" if hi > -0.01 else f"{abs(hi):.0%} below its 1-year high")
    if ix is not None:
        parts.append(f"{'beat' if ix >= 0 else 'lagged'} the index by {abs(ix):.0%} over 3 months")
    rest = ", ".join(parts[1:])
    return f"{parts[0]}. {rest[:1].upper()}{rest[1:]}."


def business_line(r, f):
    out = []
    if f is not None:
        q = num(f.get("quality_metric"))
        lab = "ROE" if str(f.get("is_financial")) == "True" else "ROCE"
        if q is not None:
            out.append(f"{lab} {q:.0%}")
        s, p = num(f.get("q_sales_yoy")), num(f.get("q_profit_yoy"))
        if s is not None and p is not None:
            out.append(f"last quarter sales {s:+.0%}, profit {p:+.0%}")
        de = num(f.get("de"))
        if de is not None and str(f.get("is_financial")) != "True":
            out.append("almost no debt" if de < 0.1 else f"debt/equity {de:.1f}")
    pe = num(r["P/E"])
    if pe:
        out.append(f"P/E {pe:.0f}")
    misses = [RULE_WORDS.get(m, m) for m in str(r["Rules missed"] or "").split(",") if m]
    line = ", ".join(out) if out else "Fundamentals not available"
    if misses:
        line += ". Weak on: " + ", ".join(dict.fromkeys(misses))
    return line + "."


def plain_reason(reason):
    reason = str(reason)
    if "no free slot" in reason:
        return "all position slots are full"
    m = re.search(r"already holding (.+)", reason)
    if m:
        return f"you already hold a {m.group(1)} stock"
    m = re.search(r"results due (\S+)", reason)
    if m:
        return f"results are due {m.group(1)}"
    if "below 1 share" in reason:
        return "one share costs more than a position slot"
    if "round-trip cost" in reason:
        return "trading costs too high for the position size"
    if "cooldown" in reason:
        return "recently stopped out of it"
    return reason


def action(r, sig, cur):
    """(full sentence, short label) for what to do."""
    es, trig, status = str(r["Entry status"]), str(r["Trigger to watch"]), str(r["Status today"])
    m = re.search(r"Breakout > \D?([\d,]+)", trig)
    piv = num(m.group(1).replace(",", "")) if m else None
    m = re.search(r"pullback to \D?([\d,]+) \(20DMA\) / \D?([\d,]+) \(50DMA\)", trig)
    p20, p50 = (num(m.group(1).replace(",", "")), num(m.group(2).replace(",", ""))) if m else (None, None)
    t = r["Ticker"]
    if es.startswith("BUY today") and t in sig:
        s = sig[t]
        return (f"BUY: limit {money(cur, num(s[11]))} (skip if it opens above), stop {money(cur, num(s[12]))}, "
                f"{s[14]} shares.", f"BUY at or below {money(cur, num(s[11]))}")
    if "skipped:" in es:
        why = plain_reason(es.split("skipped:", 1)[1])
        return f"A buy setup fired today but was not taken: {why}.", f"Setup skipped: {why}"
    if "still under its limit" in es:
        lim = num(re.search(r"limit ([\d,.]+)", es).group(1).replace(",", ""))
        return f"Recent buy setup still valid: buy at or below {money(cur, lim)}.", f"Still buyable to {money(cur, lim)}"
    if "expired" in es:
        return "Recent setup missed (price moved away). Wait for the next pullback.", "Missed: wait for pullback"
    if status.startswith("waiting: trend - 4.3"):
        if not str(r["50/200 cross"]).startswith("50DMA"):        # already crossed (recently or 1y+ ago)
            return ("US rules buy only within 65 trading days of the golden cross; this trend is older. "
                    "Watch only."), "Watch only (US: trend too old)"
        return ("US rules need the 50-day avg to cross above the 200-day avg first. Watch for the cross."), \
            "Watch for golden cross"
    if status.startswith("waiting: trend - 4."):
        return "Lagging the market on US rules. Watch only.", "Watch only (lagging)"
    val = " Valuation is above its normal range, so prefer a dip." if "valuation" in status else ""
    if es.startswith("Near breakout"):
        return (f"Close to a breakout: buy on a close above {money(cur, piv)} with strong volume.{val}",
                f"Near breakout {money(cur, piv)}")
    if "20DMA pullback zone" in es:
        return (f"In the buy zone near its 20-day avg ({money(cur, p20)}): buy if it closes above the previous "
                f"day's high.{val}", f"Buy zone (20-day avg {money(cur, p20)})")
    if "50DMA pullback zone" in es:
        return (f"In the buy zone near its 50-day avg ({money(cur, p50)}): buy if it closes above the previous "
                f"day's high.{val}", f"Buy zone (50-day avg {money(cur, p50)})")
    if es.startswith("Extended"):
        return (f"Stretched far above its 20-day avg: don't chase; wait for a dip toward {money(cur, p20)}.{val}",
                "Stretched: wait for a dip")
    if es.startswith("Below 50DMA"):
        return (f"Dipped below its 50-day avg: wait for a close back above {money(cur, p50)}.{val}",
                f"Wait for close > {money(cur, p50)}")
    return (f"Wait: buy on a close above {money(cur, piv)} (breakout) or on a bounce from about {money(cur, p20)} "
            f"(20-day avg).{val}", f"Wait: above {money(cur, piv)} or dip to {money(cur, p20)}")


# --------------------------------------------------------------------------- drawing
def chart(ax, d, r, cur):
    c = d["Close"]
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    x = d.index[-252:]                                   # 1 year
    ax.plot(x, c.loc[x], color=INK, lw=1.1)
    ax.plot(x, s50.loc[x], color=C50, lw=1.8)
    ax.plot(x, s200.loc[x], color=C200, lw=1.8)
    up = pd.to_datetime(r["In uptrend since"], errors="coerce")
    if not pd.isna(up):
        ax.axvspan(max(up, x[0]), x[-1], color=UP, alpha=0.10, lw=0)
    ax.annotate(money(cur, c.iloc[-1]), (x[-1], c.iloc[-1]), xytext=(4, 0), textcoords="offset points",
                va="center", fontsize=7.5, fontweight="bold", color=INK)
    ax.set_xlim(x[0], x[-1] + pd.Timedelta(days=20))
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{cur}{v:,.0f}"))
    ax.tick_params(labelsize=7, length=0)


LEGEND = [Line2D([], [], color=INK, lw=1.1, label="Price"),
          Line2D([], [], color=C50, lw=1.8, label="50-day avg"),
          Line2D([], [], color=C200, lw=1.8, label="200-day avg"),
          Patch(color=UP, alpha=0.25, label="Uptrend period")]


def stock_block(fig, top, d, r, f, sig, cur):
    tt = fig.text(0.07, top, r["Ticker"].replace(".NS", ""), fontsize=12, fontweight="bold", color=INK, va="top")
    bb = tt.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    fig.text(bb.x1 + 0.015, top - 0.003, f"{r['Sector']}  |  {r['Trend stage']}", fontsize=8, color=INK2,
             va="top")
    ax = fig.add_axes([0.09, top - 0.190, 0.82, 0.160])
    chart(ax, d, r, cur)
    y = top - 0.212
    full, _ = action(r, sig, cur)
    for label, text in [("Trend", trend_line(r)), ("Business", business_line(r, f)), ("Action", full)]:
        wr = textwrap.wrap(text, 100 if label == "Action" else 112)[:2]
        fig.text(0.07, y, label, fontsize=7.5, fontweight="bold", color=INK2 if label != "Action" else INK, va="top")
        fig.text(0.16, y, "\n".join(wr), fontsize=7.5, color=INK, va="top", linespacing=1.3,
                 fontweight="bold" if label == "Action" else "normal")
        y -= 0.0125 * len(wr) + 0.005


def cover(pdf, sections, sessions, summary):
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.07, 0.945, "Bullish momentum", fontsize=20, fontweight="bold", color=INK)
    fig.text(0.07, 0.922, "  |  ".join(f"{m.upper()}: close of {s}" for m, s in sessions.items()),
             fontsize=9, color=INK2)
    intro = ("New momentum = an uptrend that just started (50-day avg crossed above the 200-day avg in the last "
             "30 trading days, or about to). Ongoing = an established uptrend. Top 5 of each by score (business "
             "quality, growth and price strength). Charts show 1 year; the green band is the current uptrend. "
             "Not investment advice.")
    fig.text(0.07, 0.895, "\n".join(textwrap.wrap(intro, 118)), fontsize=8, color=INK, va="top", linespacing=1.4)
    y = 0.835
    for title, df, cur, sig in sections:
        fig.text(0.07, y, title, fontsize=11, fontweight="bold", color=INK, va="top")
        y -= 0.022
        for xx, hh in zip([0.07, 0.24, 0.47], ["Stock", "Trend", "Action"]):
            fig.text(xx, y, hh, fontsize=7.5, color=INK2, fontweight="bold", va="top")
        y -= 0.015
        if not len(df):
            fig.text(0.07, y, "None today", fontsize=8, color=INK2, va="top")
            y -= 0.015
        for _, r in df.iterrows():
            short = action(r, sig, cur)[1]
            vals = [r["Ticker"].replace(".NS", ""), f"{r['Trend stage']}, {r['Days in stage']} days", short]
            for xx, vv in zip([0.07, 0.24, 0.47], vals):
                fig.text(xx, y, vv, fontsize=8, color=INK, va="top",
                         fontweight="bold" if vv.startswith("BUY") else "normal")
            summary.append(f"  {vals[0]:<14} {vals[1]:<28} {short}")
            y -= 0.0155
        y -= 0.018
        summary.append("")
    pdf.savefig(fig)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--markets", default="india,us")
    a = ap.parse_args()
    src = qc.YahooSource()
    sections, sessions, blocks = [], {}, []
    for m in a.markets.split(","):
        cur = "₹" if m == "india" else "$"
        sess, board, sig = load_latest(m)
        sessions[m] = sess
        fund = load_fund(m)
        new, ongoing = pick(board, a.top)
        want = list(new["Ticker"]) + list(ongoing["Ticker"])
        px = src.prices(want, period="2y")
        missing = [t for t in want if t not in px]
        if missing:                                         # Yahoo sometimes drops a few; retry once
            px.update(src.prices(missing, period="2y"))
        label = "India" if m == "india" else "US"
        cfg = importlib.import_module(f"{m}_scan").CONFIG
        for title, df in [(f"{label}: new momentum", new), (f"{label}: ongoing momentum", ongoing)]:
            df = df[df["Ticker"].map(lambda t: t in px and len(px[t]) >= 252)].copy()
            for i, r in df.iterrows():
                # stage dates from the same prices the chart draws, so cover, text and shading agree
                h = qc.stage_history(qc.trend_stages(px[r["Ticker"]]["Close"].to_frame("x"), cfg)["stage"]["x"])
                if h["up_since"]:
                    df.at[i, "In uptrend since"], df.at[i, "Days in uptrend"] = str(h["up_since"]), h["days_up"]
                if h["days_in_stage"]:
                    df.at[i, "Days in stage"] = h["days_in_stage"]
            sections.append((title, df, cur, sig))
            for _, r in df.iterrows():
                f = fund.loc[r["Ticker"]].to_dict() if r["Ticker"] in fund.index else None
                blocks.append((title, px[r["Ticker"]], r, f, sig, cur))
    out = BASE / "reports" / "momentum"
    out.mkdir(parents=True, exist_ok=True)
    stem = max(sessions.values())
    path = out / f"{stem}.pdf"
    summary = [f"Bullish momentum ({', '.join(f'{k.upper()} {v}' for k, v in sessions.items())})", ""]
    with PdfPages(path) as pdf:
        idx = len(summary)
        cover(pdf, sections, sessions, summary)
        for i in range(0, len(blocks), 3):
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.legend(handles=LEGEND, loc="upper right", bbox_to_anchor=(0.93, 0.985), ncols=4, frameon=False,
                       fontsize=7.5, handlelength=1.8)
            for j, (title, d, r, f, sig, cur) in enumerate(blocks[i:i + 3]):
                top = 0.955 - j * 0.31
                fig.text(0.93, top + 0.002, title, fontsize=7, color=MUTED, ha="right", va="top")
                stock_block(fig, top, d, r, f, sig, cur)
            pdf.savefig(fig)
            plt.close(fig)
        pdf.infodict()["Title"] = "Bullish momentum"
    # section headings into the text summary
    lines, k = summary[:idx], idx
    for title, df, _, _ in sections:
        lines.append(title.upper())
        lines += summary[k:k + max(len(df), 0) + 1]
        k += len(df) + 1
    (out / f"{stem}.txt").write_text("\n".join(lines))
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
