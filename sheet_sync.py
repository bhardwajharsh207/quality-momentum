"""
Write a scan's sheet rows straight to the Google Sheet (run by GitHub Actions after each scan).

    python sheet_sync.py india        # sync any new sessions for India
    python sheet_sync.py us

Needs a Google service account key in the GOOGLE_SA_JSON environment variable (a GitHub secret), and the
sheet shared with that service account's email as Editor. Without the key it prints a note and exits 0,
and the Claude scheduled task writes the sheet instead.

For each data/<market>/sheet_rows/<session>.json not yet in the Runs tab (newest 5 checked, oldest first):
  - append each tab in "rows" (Runs last, so a failed sync retries cleanly),
  - for the newest file only, clear and rewrite each "replace" range (Board tab, this market's Today block).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

SHEET_ID = os.environ.get("QM_SHEET_ID", "1hz9yxg1Qz1AlbCKGCM_cieCJjIfpENdaZQGex0gE1TA")
API = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
RETIRED = {"Momentum", "Watchlist"}


def session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return AuthorizedSession(creds)


def call(s, method, url, **kw):
    r = s.request(method, url, timeout=60, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


def main(market: str):
    if not os.environ.get("GOOGLE_SA_JSON"):
        print("GOOGLE_SA_JSON not set; skipping direct sheet sync (the Claude scheduled task writes the sheet).")
        return
    s = session()
    folder = Path(__file__).resolve().parent / "data" / market / "sheet_rows"
    files = sorted(folder.glob("*.json"))[-5:]
    runs = call(s, "GET", f"{API}/values/{quote('Runs!A:B')}").get("values", [])
    done = {(r[0], r[1]) for r in runs if len(r) >= 2}
    todo = [f for f in files if (market.upper(), f.stem) not in done]
    if not todo:
        print(f"{market}: sheet already up to date ({files[-1].stem if files else 'no files'}).")
        return
    for f in todo:
        d = json.loads(f.read_text())
        tabs = [t for t in d["rows"] if t not in RETIRED and t != "Runs"] + ["Runs"]
        for tab in tabs:
            vals = d["rows"].get(tab) or []
            if vals:
                call(s, "POST", f"{API}/values/{quote(repr_tab(tab) + '!A1')}:append",
                     params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                     json={"values": vals})
        if f == files[-1]:
            for tab, spec in (d.get("replace") or {}).items():
                call(s, "POST", f"{API}/values/{quote(spec['clear'])}:clear", json={})
                call(s, "PUT", f"{API}/values/{quote(spec['start'])}", params={"valueInputOption": "USER_ENTERED"},
                     json={"values": spec["values"]})
        print(f"{market}: synced session {f.stem}")


def repr_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


if __name__ == "__main__":
    main(sys.argv[1])
