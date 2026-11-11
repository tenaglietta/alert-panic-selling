#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alert panic selling
===================
Controlla i titoli del paniere e invia un'email quando un titolo
scende oltre la soglia (default -7%) rispetto alla chiusura precedente.

Dati: Yahoo Finance (gratuito, tramite la libreria yfinance).
Email: Gmail SMTP con app password.
Esecuzione: pensato per GitHub Actions (vedi .github/workflows/alert.yml).

Variabili d'ambiente (impostate dal workflow, mai scritte nel codice):
  EMAIL_TO            indirizzo Gmail che invia e riceve le notifiche
  EMAIL_APP_PASSWORD  app password di Gmail (non la password normale)
  ALERT_EMAIL_TO      (facoltativo) destinatario diverso dal mittente
  THRESHOLD_PCT       soglia di ribasso, default "-7"
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

# ------------------------------------------------------------------
# CONFIGURAZIONE
# ------------------------------------------------------------------

# Paniere: titoli ad alto beta e piu' discussi sui social.
# Definito a novembre 2026, 40 titoli. Il download avviene in blocchi da 50,
# quindi il paniere puo' crescere fino a qualche centinaio di titoli senza
# modifiche al codice. Per toglierne uno: cancella la riga.
# Per aggiungerne uno: aggiungi "TICKER": "Nome societa'".
WATCHLIST = {
    # Semiconduttori / AI
    "NVDA": "NVIDIA",
    "AMD": "Advanced Micro Devices",
    "INTC": "Intel",
    "MU": "Micron Technology",
    "CRWV": "CoreWeave",
    "NBIS": "Nebius Group",
    "SMCI": "Super Micro Computer",
    "SNDK": "SanDisk",
    "MRVL": "Marvell Technology",
    "WOLF": "Wolfspeed",
    # Retail / social
    "TSLA": "Tesla",
    "GME": "GameStop",
    "PLTR": "Palantir",
    "HOOD": "Robinhood",
    "RDDT": "Reddit",
    "SOFI": "SoFi Technologies",
    "SQ": "Block",
    "APP": "AppLovin",
    "AI": "C3.ai",
    "SOUN": "SoundHound AI",
    # Spazio / quantum / nucleare / droni
    "RKLB": "Rocket Lab",
    "LUNR": "Intuitive Machines",
    "IONQ": "IonQ",
    "RGTI": "Rigetti Computing",
    "OKLO": "Oklo",
    "SMR": "NuScale Power",
    "QUBT": "Quantum Computing",
    "ASTS": "AST SpaceMobile",
    "ACHR": "Archer Aviation",
    "JOBY": "Joby Aviation",
    # Auto elettriche
    "RIVN": "Rivian",
    "LCID": "Lucid",
    "NIO": "NIO",
    # Crypto-adjacent
    "MSTR": "Strategy (ex MicroStrategy)",
    "COIN": "Coinbase",
    "MARA": "MARA Holdings",
    "RIOT": "Riot Platforms",
    "CLSK": "CleanSpark",
    # Power per l'AI
    "VST": "Vistra",
    "CEG": "Constellation Energy",
}

THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "-7"))
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "").strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------------
# STATO (evita di mandare lo stesso alert piu' volte al giorno)
# ------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"alerts": {}, "last_check": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ------------------------------------------------------------------
# LOGICA
# ------------------------------------------------------------------

def market_open(now_et):
    """True se il mercato USA e' aperto (lun-ven 9:30-16:00 ora di New York)."""
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def fetch_data():
    """Scarica gli ultimi 5 giorni di chiusure per il paniere e il cambio EUR/USD.

    I titoli vengono scaricati in blocchi da 50: Yahoo risponde male alle
    richieste troppo grandi, cosi' il paniere puo' arrivare a qualche
    centinaio di titoli senza altre modifiche.
    """
    tickers = list(WATCHLIST)
    frames = []
    for i in range(0, len(tickers), 50):
        block = tickers[i : i + 50]
        frames.append(
            yf.download(
                block,
                period="5d",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        )
    if len(frames) == 1:
        data = frames[0]
    else:
        data = pd.concat(frames, axis=1)

    rows = {}
    for ticker in WATCHLIST:
        try:
            closes = data[ticker]["Close"].dropna()
            if len(closes) < 2:
                print(f"ATTENZIONE: serie troppo corta per {ticker}, lo salto.")
                continue
            rows[ticker] = {"prev": float(closes.iloc[-2]), "cur": float(closes.iloc[-1])}
        except (KeyError, TypeError, IndexError):
            print(f"ATTENZIONE: dati non disponibili per {ticker}, lo salto.")

    try:
        eurusd = float(
            yf.download(
                "EURUSD=X",
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )["Close"]
            .dropna()
            .iloc[-1]
        )
    except (KeyError, TypeError, IndexError):
        eurusd = None  # nessuna conversione in euro se il cambio non risponde
    return rows, eurusd


def eur(price, eurusd):
    return price / eurusd if eurusd else None


def build_email(alerts, table, eurusd, now_et):
    dest = os.environ.get("ALERT_EMAIL_TO", "").strip() or EMAIL_TO

    lines = [f"ALLERTA PANIC SELLING - {now_et.strftime('%d/%m/%Y %H:%M')} (ora USA)", ""]
    for ticker, change, row in alerts:
        prev_eur = eur(row["prev"], eurusd)
        cur_eur = eur(row["cur"], eurusd)
        lines.append(f"{ticker} - {WATCHLIST[ticker]}: {change:+.2f}%")
        lines.append(
            f"  Chiusura precedente: {row['prev']:.2f} USD"
            + (f" (circa {prev_eur:.2f} EUR)" if prev_eur else "")
        )
        lines.append(
            f"  Prezzo attuale:      {row['cur']:.2f} USD"
            + (f" (circa {cur_eur:.2f} EUR)" if cur_eur else "")
        )
        lines.append("")

    lines += [
        "Prima di trattarlo come panic selling meccanico, verifica se esiste",
        "una notizia fondamentale reale (earnings, guidance tagliata, indagini,",
        "vendite di azionisti rilevanti). Un -7% su notizia reale non si",
        "rimbalza con la stessa affidabilita'.",
        "",
        "Situazione dell'intero paniere:",
    ]
    for ticker, change, _ in sorted(table, key=lambda x: x[1]):
        lines.append(f"  {ticker:<6} {change:+7.2f}%")
    body = "\n".join(lines)

    subject = "[ALERT] " + ", ".join(f"{t} {c:+.1f}%" for t, c, _ in alerts)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_TO
    msg["To"] = dest
    return msg, dest


def send_email(msg, dest):
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_TO, EMAIL_APP_PASSWORD)
        server.send_message(msg)
    print(f"Email inviata a {dest}.")


def main():
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")

    if not EMAIL_TO or not EMAIL_APP_PASSWORD:
        print("ERRORE: mancano EMAIL_TO ed EMAIL_APP_PASSWORD (seguire la guida).")
        sys.exit(1)

    # Crea o aggiorna lo stato a ogni esecuzione, anche a mercato chiuso:
    # cosi' state.json esiste sempre e GitHub mantiene attive le azioni programmate.
    state = load_state()
    state["last_check"] = today
    save_state(state)

    if not market_open(now_et):
        print("Mercato USA chiuso: nessun controllo da fare.")
        return

    rows, eurusd = fetch_data()
    if not rows:
        print("ERRORE: nessun dato ricevuto da Yahoo Finance.")
        sys.exit(1)

    alerts = []
    table = []
    for ticker, row in rows.items():
        change = (row["cur"] / row["prev"] - 1.0) * 100.0
        table.append((ticker, change, row))
        if change <= THRESHOLD_PCT and state["alerts"].get(ticker) != today:
            alerts.append((ticker, change, row))
            state["alerts"][ticker] = today

    if alerts:
        msg, dest = build_email(alerts, table, eurusd, now_et)
        send_email(msg, dest)
        for ticker, change, _ in alerts:
            print(f"ALERT: {ticker} {change:+.2f}%")
    else:
        print(f"Nessun titolo sotto {THRESHOLD_PCT}%. Controllati {len(rows)} titoli.")

    save_state(state)


if __name__ == "__main__":
    main()
