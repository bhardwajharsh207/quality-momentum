"""
Email the latest momentum PDF (run by the US scan workflow after a new session).

Needs two GitHub secrets: GMAIL_ADDRESS (the sending Gmail account) and GMAIL_APP_PASSWORD (a Gmail
app password, not the normal password). EMAIL_TO defaults to GMAIL_ADDRESS. Without them it prints a note
and exits 0.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; skipping email.")
        return
    pdfs = sorted((BASE / "reports" / "momentum").glob("*.pdf"))
    if not pdfs:
        print("No momentum PDF found; skipping email.")
        return
    pdf = pdfs[-1]
    txt = pdf.with_suffix(".txt")
    summary = txt.read_text() if txt.exists() else ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sheet = "https://docs.google.com/spreadsheets/d/1hz9yxg1Qz1AlbCKGCM_cieCJjIfpENdaZQGex0gE1TA"
    msg = EmailMessage()
    msg["Subject"] = f"Momentum stocks: {pdf.stem}"
    msg["From"] = user
    msg["To"] = os.environ.get("EMAIL_TO") or user
    msg.set_content(f"{summary}\n\nCharts for each stock are in the attached PDF.\nSheet: {sheet}\n"
                    + (f"Repo: https://github.com/{repo}\n" if repo else "")
                    + "\nNot investment advice.")
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=f"momentum-{pdf.stem}.pdf")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"Emailed {pdf.name} to {msg['To']}")


if __name__ == "__main__":
    main()
