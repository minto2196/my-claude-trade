#!/usr/bin/env python3
"""Auto-trader for the Trade Desk: acts on the verification agent's flags in your Alpaca account.

THIS PLACES REAL ORDERS when ALPACA_PAPER is false. It does nothing unless you switch it on.

Kill switch: the AUTOTRADE repository variable (or secret).
  off        (default, also when unset or anything unrecognised)  no orders at all
  sell-only  no new buys; exits below still run
  on         buys and exits

Buying (only while the US market is open):
  * only stocks the agent rates 4 of 4 green and that you don't already hold
  * only operating companies (funds/ETFs skipped unless AUTOTRADE_ALLOW_ETF=true)
  * the account is split into AUTOTRADE_SLOTS equal slots (default 12, so 8.33% each);
    each buy is one slot of account value, paid from cash only (never margin),
    fractional where Alpaca allows it, never below $1
  * at most AUTOTRADE_SLOTS positions owned by the auto-trader at once
  * no buys on a day the account is down AUTOTRADE_DAILY_LOSS_PCT (default 5%) or more

Exits (only on positions the auto-trader bought itself; your manual positions are never touched):
  * 2 or fewer green           -> sell all
  * 3 green                    -> sell half, once (not again within 3 days)
  * down STOP_LOSS_PCT  (8%)   -> sell all
  * up   TAKE_PROFIT_PCT (20%) -> sell all, freeing the slot for a new 4-green stock
  * never sells something bought the same day (avoids pattern-day-trade flags on small accounts)

Usage: python3 autotrader.py OUT.json --agent agent.json [--previous URL_OR_PATH]
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("ALPACA_API_KEY", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"
TRADE = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"

MODE = os.environ.get("AUTOTRADE", "off").strip().lower()
MODE = MODE if MODE in ("on", "sell-only") else "off"
SLOTS = max(1, int(os.environ.get("AUTOTRADE_SLOTS", "12") or 12))
STOP_LOSS_PCT = float(os.environ.get("AUTOTRADE_STOP_LOSS_PCT", "8") or 8)
TAKE_PROFIT_PCT = float(os.environ.get("AUTOTRADE_TAKE_PROFIT_PCT", "20") or 20)
DAILY_LOSS_PCT = float(os.environ.get("AUTOTRADE_DAILY_LOSS_PCT", "5") or 5)
ALLOW_ETF = os.environ.get("AUTOTRADE_ALLOW_ETF", "false").strip().lower() == "true"
MAX_AGENT_AGE_MIN = 30
TAG = "tdagent-"
FUND_WORDS = re.compile(r"\b(ETF|ETN|FUND|TRUST|ISHARES|SPDR|PROSHARES|DIREXION|VANGUARD|INVESCO QQQ|SELECT SECTOR|ULTRA|INVERSE|2X|3X|LEVERAGED)\b", re.I)


def call(method, path, params=None, body=None):
    url = TRADE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET,
        "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3 + attempt * 3)
                continue
            msg = e.read().decode()[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {msg}")


def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def load(src):
    if not src:
        return {}
    try:
        if src.startswith("http"):
            with urllib.request.urlopen(src, timeout=20) as r:
                return json.loads(r.read().decode())
        with open(src) as f:
            return json.load(f)
    except Exception:
        return {}


def et_today():
    # US Eastern date without tz database: UTC-4 (EDT) / UTC-5 (EST); a few hours' error near midnight is harmless here
    now = dt.datetime.now(dt.timezone.utc)
    return (now - dt.timedelta(hours=4)).date().isoformat()


def run(agent, previous):
    now = dt.datetime.now(dt.timezone.utc)
    out = {"generated_at": now.isoformat(timespec="seconds"), "mode": MODE, "paper": PAPER,
           "rules": {"slots": SLOTS, "slot_pct": round(100 / SLOTS, 2), "stop_loss_pct": STOP_LOSS_PCT,
                     "take_profit_pct": TAKE_PROFIT_PCT, "daily_loss_pct": DAILY_LOSS_PCT, "allow_etf": ALLOW_ETF},
           "actions": [], "notes": []}
    history = previous.get("history", [])
    if MODE == "off":
        out["notes"].append("Switched off. No orders placed. Set AUTOTRADE to on (or sell-only) to start.")
        out["history"] = history
        return out

    account = call("GET", "/v2/account")
    clock = call("GET", "/v2/clock")
    positions = {p["symbol"]: p for p in call("GET", "/v2/positions")}
    orders = call("GET", "/v2/orders", {"status": "all", "limit": 500, "direction": "desc",
                                         "after": (now - dt.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    equity, cash = num(account.get("equity")), num(account.get("cash"))
    last_equity = num(account.get("last_equity"), equity)
    out["account"] = {"equity": equity, "cash": cash}
    out["market_open"] = bool(clock.get("is_open"))

    if account.get("trading_blocked") or account.get("account_blocked"):
        out["notes"].append("Alpaca reports trading is blocked on this account. Nothing placed.")
        out["history"] = history
        return out
    if not clock.get("is_open"):
        out["notes"].append(f"Market closed. Next open {clock.get('next_open', '?')}. Orders wait until then.")
        out["history"] = history
        return out
    age = (now - dt.datetime.fromisoformat(agent.get("generated_at", "1970-01-01T00:00:00+00:00"))).total_seconds() / 60
    if agent.get("error") or not agent.get("board") or age > MAX_AGENT_AGE_MIN:
        out["notes"].append("The verification agent's results are missing, failed or stale. No orders this run.")
        out["history"] = history
        return out

    today = et_today()
    mine = [o for o in orders if (o.get("client_order_id") or "").startswith(TAG)]
    owned = {o["symbol"] for o in mine if o.get("side") == "buy" and o.get("status") in ("filled", "partially_filled")}
    owned &= set(positions)
    open_syms = {o["symbol"] for o in orders if o.get("status") in ("new", "accepted", "pending_new", "partially_filled", "held")}
    bought_today = {o["symbol"] for o in mine if o.get("side") == "buy" and (o.get("filled_at") or "")[:10] == today}
    trimmed_recently = {o["symbol"] for o in mine if o.get("side") == "sell" and o.get("status") == "filled"
                        and (now - dt.datetime.fromisoformat(o["filled_at"].replace("Z", "+00:00"))).days < 3}

    cols = agent.get("board_columns", [])
    board = {r[cols.index("symbol")]: dict(zip(cols, r)) for r in agent.get("board", [])}

    def place(sym, side, reason, notional=None, qty=None):
        body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": f"{TAG}{side}-{sym}-{int(time.time()*1000)}"[:48]}
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        act = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "symbol": sym, "side": side,
               "notional": body.get("notional"), "qty": body.get("qty"), "reason": reason}
        try:
            o = call("POST", "/v2/orders", body=body)
            act.update(status=o.get("status"), order_id=o.get("id"))
        except Exception as e:
            act.update(status="rejected", error=str(e)[:200])
        out["actions"].append(act)
        return act

    # ---- exits first (they free cash and slots)
    for sym in sorted(owned):
        if sym in open_syms or sym in bought_today:
            continue
        p = positions[sym]
        qty, plpc = num(p.get("qty")), num(p.get("unrealized_plpc")) * 100
        b = board.get(sym)
        greens = b["greens"] if b else None
        if plpc <= -STOP_LOSS_PCT:
            place(sym, "sell", f"Stop-loss: down {abs(plpc):.1f}% from entry", qty=qty)
        elif plpc >= TAKE_PROFIT_PCT:
            place(sym, "sell", f"Take profit: up {plpc:.1f}% from entry", qty=qty)
        elif greens is not None and greens <= 2:
            place(sym, "sell", f"Only {greens} of 4 green", qty=qty)
        elif greens == 3 and sym not in trimmed_recently:
            half = math.floor(qty / 2 * 1e9) / 1e9 if p.get("asset_class") == "crypto" or qty != int(qty) else math.floor(qty / 2)
            if half > 0:
                place(sym, "sell", "3 of 4 green: start selling (half)", qty=half)

    # ---- buys
    if MODE != "on":
        out["notes"].append("Sell-only: exits run, no new buys.")
    elif last_equity and (equity / last_equity - 1) * 100 <= -DAILY_LOSS_PCT:
        out["notes"].append(f"Account down {(equity/last_equity-1)*100:.1f}% today. No new buys until tomorrow.")
    else:
        slot = equity / SLOTS
        free = SLOTS - len(owned)
        spend = cash
        candidates = [s for s in agent.get("symbols", []) if s.get("signal") == "buy" and s.get("greens") == 4]
        candidates.sort(key=lambda s: s.get("rank") or 999)
        bought = 0
        for c in candidates:
            if bought >= free:
                break
            sym = c["symbol"]
            if sym in positions or sym in open_syms or board.get(sym, {}).get("greens") != 4:
                continue
            try:
                asset = call("GET", f"/v2/assets/{urllib.parse.quote(sym)}")
            except Exception:
                continue
            if not asset.get("tradable"):
                continue
            if not ALLOW_ETF and FUND_WORDS.search(asset.get("name") or ""):
                out["notes"].append(f"Skipped {sym}: looks like a fund ({asset.get('name')}).")
                continue
            amount = math.floor(min(slot, spend) * 100) / 100
            if asset.get("fractionable"):
                if amount < 1:
                    out["notes"].append("Not enough cash for another $1 order.")
                    break
                act = place(sym, "buy", f"4 of 4 green, rank #{c.get('rank')}, one slot ({100/SLOTS:.1f}%)", notional=amount)
            else:
                price = num(c.get("price"))
                shares = math.floor(amount / price) if price else 0
                if shares < 1:
                    out["notes"].append(f"Skipped {sym}: not fractionable and one share costs more than a slot.")
                    continue
                amount = shares * price
                act = place(sym, "buy", f"4 of 4 green, rank #{c.get('rank')}, {shares} whole share(s)", qty=shares)
            if act.get("status") != "rejected":
                spend -= amount
                bought += 1
        if not candidates:
            out["notes"].append("No stock has 4 of 4 green right now. Nothing to buy.")
        elif bought == 0 and free <= 0:
            out["notes"].append(f"All {SLOTS} slots are full.")

    out["owned"] = sorted(owned)
    out["history"] = (history + out["actions"])[-300:]
    if not out["actions"]:
        out["notes"].append("No orders needed this run.")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--previous")
    a = ap.parse_args()
    previous = load(a.previous)
    agent = load(a.agent)
    if not (KEY and SECRET):
        out = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": MODE,
               "notes": ["Alpaca keys missing."], "actions": [], "history": previous.get("history", [])}
    else:
        try:
            out = run(agent, previous)
        except Exception as e:
            out = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": MODE,
                   "error": f"{type(e).__name__}: {e}"[:300], "actions": [], "notes": [], "history": previous.get("history", [])}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"autotrader mode={out.get('mode')} actions={len(out.get('actions', []))} "
          f"notes={out.get('notes')} error={out.get('error')}")


if __name__ == "__main__":
    main()
