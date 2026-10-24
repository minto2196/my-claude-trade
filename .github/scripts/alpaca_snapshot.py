#!/usr/bin/env python3
"""Write a read-only snapshot of an Alpaca account for the dashboard.

Reads ALPACA_API_KEY / ALPACA_SECRET_KEY (and ALPACA_PAPER, default "true")
from the environment, calls Alpaca's REST API, and writes one JSON file.
It only reads: no orders are placed or changed. Standard library only.

Usage: python3 alpaca_snapshot.py OUTPUT.json
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("ALPACA_API_KEY", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"
TRADE = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"


def get(base, path, params=None):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def crypto_pair(symbol):
    if "/" in symbol:
        return symbol
    for quote in ("USDT", "USDC", "USD", "BTC"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)] + "/" + quote
    return symbol


def daily_closes(stocks, cryptos):
    """{symbol: [close, ...]} oldest first, ~35 calendar days of daily bars."""
    start = (dt.datetime.utcnow() - dt.timedelta(days=35)).strftime("%Y-%m-%dT00:00:00Z")
    out = {}
    if stocks:
        token = None
        while True:
            p = {"symbols": ",".join(stocks), "timeframe": "1Day", "start": start,
                 "feed": "iex", "adjustment": "all", "limit": 10000}
            if token:
                p["page_token"] = token
            d = get(DATA, "/v2/stocks/bars", p)
            for s, bars in (d.get("bars") or {}).items():
                out.setdefault(s, []).extend(b["c"] for b in bars)
            token = d.get("next_page_token")
            if not token:
                break
    if cryptos:
        pairs = {crypto_pair(s): s for s in cryptos}
        d = get(DATA, "/v1beta3/crypto/us/bars",
                {"symbols": ",".join(pairs), "timeframe": "1Day", "start": start, "limit": 10000})
        for pair, bars in (d.get("bars") or {}).items():
            out[pairs.get(pair, pair)] = [b["c"] for b in bars]
    return out


def change_over(closes, price, n):
    """% change from the close n trading days ago to the current price."""
    if not closes or len(closes) <= n or not price:
        return None
    base = closes[-1 - n]
    return round((price / base - 1) * 100, 2) if base else None


def first_buy_dates(symbols):
    """Date of the oldest buy fill per symbol within the last ~year."""
    dates = {}
    after = (dt.datetime.utcnow() - dt.timedelta(days=365)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        acts = get(TRADE, "/v2/account/activities/FILL",
                   {"direction": "asc", "after": after, "page_size": 100})
    except urllib.error.HTTPError:
        return dates
    for a in acts:
        sym = a.get("symbol", "").replace("/", "")
        if sym in symbols and a.get("side") == "buy" and sym not in dates:
            dates[sym] = (a.get("transaction_time") or "")[:10]
    return dates


def build():
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not (KEY and SECRET):
        return {"configured": False, "generated_at": now,
                "message": "Add ALPACA_API_KEY and ALPACA_SECRET_KEY as repository secrets."}

    account = get(TRADE, "/v2/account")
    positions = get(TRADE, "/v2/positions")
    clock = get(TRADE, "/v2/clock")
    hist = get(TRADE, "/v2/account/portfolio/history", {"period": "1A", "timeframe": "1D"})

    stocks = [p["symbol"] for p in positions if p.get("asset_class") != "crypto"]
    cryptos = [p["symbol"] for p in positions if p.get("asset_class") == "crypto"]
    try:
        closes = daily_closes(stocks, cryptos)
    except urllib.error.HTTPError:
        closes = {}
    bought = first_buy_dates(set(stocks + cryptos))

    rows = []
    for p in positions:
        sym = p["symbol"]
        price = num(p.get("current_price"))
        c = closes.get(sym, [])
        rows.append({
            "symbol": sym,
            "asset_class": p.get("asset_class"),
            "qty": num(p.get("qty")),
            "side": p.get("side"),
            "avg_entry": num(p.get("avg_entry_price")),
            "price": price,
            "market_value": num(p.get("market_value")),
            "bought": bought.get(sym),
            "today_pct": round(num(p.get("change_today")) * 100, 2),
            "d5_pct": change_over(c, price, 5),
            "d15_pct": change_over(c, price, 15),
            "pl": num(p.get("unrealized_pl")),
            "pl_pct": round(num(p.get("unrealized_plpc")) * 100, 2),
            "pl_today": num(p.get("unrealized_intraday_pl")),
        })
    rows.sort(key=lambda r: -abs(r["market_value"]))

    history = []
    for ts, eq in zip(hist.get("timestamp") or [], hist.get("equity") or []):
        if eq is not None:
            history.append([dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"), round(eq, 2)])

    equity = num(account.get("equity"))
    last_equity = num(account.get("last_equity"))
    return {
        "configured": True,
        "generated_at": now,
        "paper": PAPER,
        "account": {
            "equity": equity,
            "cash": num(account.get("cash")),
            "last_equity": last_equity,
            "today_pl": round(equity - last_equity, 2),
            "buying_power": num(account.get("buying_power")),
            "created_at": (account.get("created_at") or "")[:10],
            "base_value": num(hist.get("base_value"), equity),
        },
        "clock": {k: clock.get(k) for k in ("timestamp", "is_open", "next_open", "next_close")},
        "positions": rows,
        "history": history,
    }


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "portfolio.json"
    try:
        snap = build()
    except Exception as e:  # keep the site working even if Alpaca is down
        snap = {"configured": True, "error": f"{type(e).__name__}: {e}"[:300],
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print(f"wrote {out}: configured={snap.get('configured')} "
          f"positions={len(snap.get('positions', []))} error={snap.get('error')}")


if __name__ == "__main__":
    main()
