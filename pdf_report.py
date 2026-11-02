"""
Build a PDF of the latest session: stocks that joined / changed stage, plus the top-ranked
names, each with a 1-month price chart.

    python pdf_report.py                 # both markets, latest session
    python pdf_report.py --top 20        # how many top-scoring stocks per market
    python pdf_report.py --fake          # synthetic prices, for testing without network

Writes reports/top-stocks-<date>.pdf
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

ROOT = Path(__file__).resolve().parent
MARKETS = {"india": {"label": "India (Nifty 500)", "cur": "₹", "board": "Board - India"},
           "us": {"label": "US (S&P 500)", "cur": "$", "board": "Board - US"}}
INK, QUIET, UP, DOWN, GRID = "#1b1b1b", "#6b6b6b", "#1a7f4b", "#b3261e", "#e3e3e3"
COLS, ROWS = 3, 4                      # charts per page


def latest_payload(market):
    files = sorted(glob.glob(str(ROOT / f"data/{market}/sheet_rows/*.json")))
    if not files:
        return None, None
    p = Path(files[-1])
    return json.loads(p.read_text()), p.stem


def board_frame(payload, market):
    blk = (payload.get("replace") or {}).get(MARKETS[market]["board"])
    if not blk or not blk.get("values"):
        return pd.DataFrame()
    v = blk["values"]
    return pd.DataFrame(v[1:], columns=v[0])


def changes_frame(payload):
    H = payload["headers"].get("Changes")
    R = payload["rows"].get("Changes") or []
    return pd.DataFrame(R, columns=H) if H and R else pd.DataFrame()


def pick(payload, market, top):
    """One row per ticker: everything that changed today, then the top scorers."""
    board, ch = board_frame(payload, market), changes_frame(payload)
    if len(board):
        board["Score"] = pd.to_numeric(board["Score"], errors="coerce")
    notes, order = {}, []
    for _, r in ch.iterrows():
        notes.setdefault(r["Ticker"], []).append(str(r["Event"]))
        if r["Ticker"] not in order:
            order.append(r["Ticker"])
    changed = set(order)
    rest = [t for t in (board.sort_values("Score", ascending=False)["Ticker"] if len(board) else [])
            if t not in changed][:top]
    binfo = board.set_index("Ticker").to_dict("index") if len(board) else {}
    cinfo = ch.drop_duplicates("Ticker").set_index("Ticker").to_dict("index") if len(ch) else {}
    out = []
    for t in order + rest:
        b, c = binfo.get(t, {}), cinfo.get(t, {})
        out.append({
            "ticker": t,
            "section": "Changed today" if t in changed else "Top ranked",
            "events": notes.get(t, []),
            "sector": b.get("Sector") or c.get("Sector") or "",
            "stage": b.get("Trend stage") or c.get("To") or "",
            "days": b.get("Days in uptrend"),
            "score": b.get("Score", c.get("Score")),
            "status": b.get("Status today") or c.get("Status today") or "",
            "close": b.get("Last close") or c.get("Last close"),
        })
    return out


def load_prices(tickers, fake=False):
    if fake:
        rng = np.random.default_rng(3)
        idx = pd.bdate_range(end=dt.date.today(), periods=70)
        return {t: pd.DataFrame({"Close": 100 * np.exp(np.cumsum(rng.normal(0.001, 0.015, len(idx))))}, index=idx)
                for t in tickers}
    import yfinance as yf
    out, tick = {}, list(tickers)
    for i in range(0, len(tick), 60):
        chunk = tick[i:i + 60]
        df = yf.download(chunk, period="3mo", interval="1d", auto_adjust=True,
                         group_by="ticker", threads=True, progress=False)
        if df is None or df.empty:
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in chunk:
            try:
                d = df[t] if multi else df
            except KeyError:
                continue
            d = d[["Close"]].dropna()
            if len(d) > 5:
                d.index = pd.to_datetime(d.index).tz_localize(None)
                out[t] = d
    return out


def panel(ax, row, px, cur):
    t = row["ticker"]
    d = px.get(t)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    if d is None or len(d) < 5:
        ax.text(0.5, 0.5, "no price data", ha="center", va="center", color=QUIET, fontsize=8)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(t.replace(".NS", ""), loc="left", fontsize=10, fontweight="bold", color=INK)
        return
    ma20 = d["Close"].rolling(20).mean()
    cut = d.index[-1] - pd.Timedelta(days=31)
    w, m = d[d.index >= cut], ma20[ma20.index >= cut]
    chg = float(w["Close"].iloc[-1] / w["Close"].iloc[0] - 1)
    col = UP if chg >= 0 else DOWN
    ax.plot(w.index, w["Close"], color=col, lw=1.4)
    ax.fill_between(w.index, w["Close"].min(), w["Close"], color=col, alpha=0.07)
    if m.notna().any():
        ax.plot(m.index, m, color=QUIET, lw=0.9, ls="--", label="20DMA")
    ax.set_title(f"{t.replace('.NS', '')}   {chg:+.1%}", loc="left", fontsize=10, fontweight="bold",
                 color=col, pad=14)
    sub = (row["events"][0] if row["events"] else row["stage"] or "")[:46]
    ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=6.6, color=QUIET)
    last = float(w["Close"].iloc[-1])
    ax.text(1, 1.02, f"{cur}{last:,.0f}", transform=ax.transAxes, fontsize=7.5,
            color=INK, ha="right", fontweight="bold")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.tick_params(labelsize=6.5, colors=QUIET, length=2)


def cover(pdf, sessions, picks):
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.06, 0.90, "Top stocks and stage changes", fontsize=22, fontweight="bold", color=INK)
    sess = ", ".join(f"{MARKETS[m]['label']}: {s}" for m, s in sessions.items())
    fig.text(0.06, 0.855, f"Close of {sess}. Charts show the last month of daily closes with the 20-day average.",
             fontsize=10, color=QUIET)
    y = 0.76
    for m, rows in picks.items():
        ch = [r for r in rows if r["section"] == "Changed today"]
        fig.text(0.06, y, MARKETS[m]["label"], fontsize=13, fontweight="bold", color=INK)
        y -= 0.035
        counts = {}
        for r in ch:
            for e in r["events"]:
                counts[e] = counts.get(e, 0) + 1
        for e, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            fig.text(0.08, y, f"{n:>3}   {e}", fontsize=9.5, color=INK)
            y -= 0.026
        fig.text(0.08, y, f"{len([r for r in rows if r['section'] == 'Top ranked']):>3}   "
                          "highest-scoring stocks in an uptrend (no change today)", fontsize=9.5, color=QUIET)
        y -= 0.055
    fig.text(0.06, 0.06, "Research notes, not investment advice. Signals come from the close shown; "
                         "act at the next open with a limit order.", fontsize=8, color=QUIET)
    pdf.savefig(fig); plt.close(fig)


def section_pages(pdf, title, rows, px, cur):
    per = COLS * ROWS
    for start in range(0, len(rows), per):
        chunk = rows[start:start + per]
        fig, axes = plt.subplots(ROWS, COLS, figsize=(11.69, 8.27))
        fig.subplots_adjust(left=0.05, right=0.97, top=0.88, bottom=0.07, hspace=0.62, wspace=0.22)
        page = f"  ({start // per + 1} of {(len(rows) - 1) // per + 1})" if len(rows) > per else ""
        fig.text(0.05, 0.945, title + page, fontsize=14, fontweight="bold", color=INK)
        for ax, row in zip(axes.flat, chunk):
            panel(ax, row, px, cur)
        for ax in axes.flat[len(chunk):]:
            ax.axis("off")
        pdf.savefig(fig); plt.close(fig)


def table_page(pdf, title, rows, cur):
    per = 22
    for start in range(0, len(rows), per):
        chunk = rows[start:start + per]
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.text(0.05, 0.945, title, fontsize=14, fontweight="bold", color=INK)
        hdr = ["Ticker", "Sector", "Stage", "Score", "Last close", "What happened / what to watch"]
        xs = [0.05, 0.16, 0.33, 0.48, 0.545, 0.63]
        for x, h in zip(xs, hdr):
            fig.text(x, 0.90, h, fontsize=8.5, fontweight="bold", color=QUIET)
        y = 0.875
        for r in chunk:
            vals = [r["ticker"].replace(".NS", ""), (r["sector"] or "")[:20], (r["stage"] or "")[:18],
                    f"{float(r['score']):.2f}" if r["score"] not in (None, "") else "",
                    f"{cur}{float(r['close']):,.0f}" if r["close"] not in (None, "") else "",
                    ((r["events"][0] if r["events"] else r["status"]) or "")[:52]]
            for x, v in zip(xs, vals):
                fig.text(x, y, v, fontsize=8, color=INK)
            y -= 0.0365
        pdf.savefig(fig); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=18)
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()

    picks, sessions = {}, {}
    for m in MARKETS:
        payload, sess = latest_payload(m)
        if not payload:
            continue
        sessions[m] = sess
        picks[m] = pick(payload, m, a.top)
    if not picks:
        raise SystemExit("no session data found")

    tickers = sorted({r["ticker"] for rows in picks.values() for r in rows})
    print(f"Fetching 3mo prices for {len(tickers)} tickers ...")
    px = load_prices(tickers, a.fake)
    print(f"  got {len(px)}")

    out = ROOT / "reports" / f"top-stocks-{max(sessions.values())}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out) as pdf:
        cover(pdf, sessions, picks)
        for m, rows in picks.items():
            cur = MARKETS[m]["cur"]
            ch = [r for r in rows if r["section"] == "Changed today"]
            tp = [r for r in rows if r["section"] == "Top ranked"]
            if ch:
                table_page(pdf, f"{MARKETS[m]['label']} - what changed at the {sessions[m]} close", ch, cur)
                section_pages(pdf, f"{MARKETS[m]['label']} - changed today", ch, px, cur)
            if tp:
                section_pages(pdf, f"{MARKETS[m]['label']} - highest-scoring stocks in an uptrend", tp, px, cur)
    print("wrote", out)


if __name__ == "__main__":
    main()
