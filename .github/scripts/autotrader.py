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
  * no buys in the last AUTOTRADE_NO_BUY_MINUTES (default 30) before the close: sells only

Exits (only on positions the auto-trader bought itself; your manual positions are never touched):
  * 2 or fewer green           -> sell all
  * 3 green                    -> sell half, once (not again within 3 days)
  * down STOP_LOSS_PCT  (5%)   -> sell all
  * trailing stop: once up TRAIL_ARM_PCT (4%), sell all if it falls TRAIL_PCT (4%) from its best price
  * bad day: down BAD_DAY_PCT (3%) today and no longer 4 of 4 green -> sell all
  * up   TAKE_PROFIT_PCT (20%) -> sell all, freeing the slot for a new 4-green stock
  * exits run the same day; on a live account under $25,000 that has used its 3 day trades,
    same-day exits wait (Alpaca's pattern-day-trader rule would reject them)

Every order is written to a trade journal (journal.json) with everything known at the moment
it was placed; journal_report.py turns that into Excel and PDF.

Usage: python3 autotrader.py OUT.json --agent agent.json [--previous URL_OR_PATH]
           [--journal journal.json --journal-previous URL_OR_PATH]
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
STOP_LOSS_PCT = float(os.environ.get("AUTOTRADE_STOP_LOSS_PCT", "5") or 5)
TRAIL_ARM_PCT = float(os.environ.get("AUTOTRADE_TRAIL_ARM_PCT", "4") or 4)
TRAIL_PCT = float(os.environ.get("AUTOTRADE_TRAIL_PCT", "4") or 4)
BAD_DAY_PCT = float(os.environ.get("AUTOTRADE_BAD_DAY_PCT", "3") or 3)
PEAKS = {}            # best price seen per owned symbol (trailing stop); loaded/saved via --state
TAKE_PROFIT_PCT = float(os.environ.get("AUTOTRADE_TAKE_PROFIT_PCT", "20") or 20)
DAILY_LOSS_PCT = float(os.environ.get("AUTOTRADE_DAILY_LOSS_PCT", "5") or 5)
NO_BUY_MINUTES = float(os.environ.get("AUTOTRADE_NO_BUY_MINUTES", "30") or 30)
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


JOURNAL = []          # new journal entries made this run


def iso_utc(x):
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00")) if x else None


def refresh_journal(entries):
    """Fill in fills for orders that weren't final last time (read-only)."""
    final = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
    for e in entries:
        if not e.get("order_id"):
            continue
        if e.get("status") in final and (e.get("status") != "filled" or e.get("filled_at_utc")):
            continue
        try:
            o = call("GET", f"/v2/orders/{e['order_id']}")
        except Exception:
            continue
        e.update(status=o.get("status"), submitted_at_utc=o.get("submitted_at") or e.get("submitted_at_utc"),
                 filled_at_utc=o.get("filled_at"), filled_qty=num(o.get("filled_qty")) or None,
                 filled_avg_price=num(o.get("filled_avg_price")) or None)
        if e.get("filled_qty") and e.get("filled_avg_price"):
            e["filled_value"] = round(e["filled_qty"] * e["filled_avg_price"], 2)
    return entries


def run(agent, previous):
    now = dt.datetime.now(dt.timezone.utc)
    out = {"generated_at": now.isoformat(timespec="seconds"), "mode": MODE, "paper": PAPER,
           "rules": {"slots": SLOTS, "slot_pct": round(100 / SLOTS, 2), "stop_loss_pct": STOP_LOSS_PCT,
                     "trail_arm_pct": TRAIL_ARM_PCT, "trail_pct": TRAIL_PCT, "bad_day_pct": BAD_DAY_PCT,
                     "take_profit_pct": TAKE_PROFIT_PCT, "daily_loss_pct": DAILY_LOSS_PCT, "allow_etf": ALLOW_ETF,
                     "no_buy_minutes_before_close": NO_BUY_MINUTES},
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

    minutes_left = None
    try:
        minutes_left = (iso_utc(clock["next_close"]) - iso_utc(clock["timestamp"])).total_seconds() / 60
    except Exception:
        pass
    closing = minutes_left is not None and minutes_left <= NO_BUY_MINUTES
    out["minutes_to_close"] = round(minutes_left) if minutes_left is not None else None
    pdt_limited = (not PAPER and equity < 25000 and int(num(account.get("daytrade_count"))) >= 3)

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
    detail = {d["symbol"]: d for d in agent.get("symbols", [])}

    def context(sym, side, reason, rule, asset=None):
        d, b, pos = detail.get(sym) or {}, board.get(sym) or {}, positions.get(sym) or {}
        ch = d.get("checks") or {}
        c1, c2, c3, c4 = (ch.get(k) or {} for k in ("price", "organic", "prediction", "news"))
        heads = c4.get("headlines") or []
        return {
            "side": side, "symbol": sym, "reason": reason, "rule": rule, "mode": "paper" if PAPER else "live",
            "company": (asset or {}).get("name") or pos.get("symbol"), "exchange": (asset or {}).get("exchange") or pos.get("exchange"),
            "asset_class": (asset or {}).get("class") or pos.get("asset_class") or "us_equity",
            "fractionable": (asset or {}).get("fractionable"),
            "account_equity_before": equity, "account_cash_before": cash, "slot_pct": round(100 / SLOTS, 2),
            "price_at_decision": d.get("price") or b.get("price") or num(pos.get("current_price")) or None,
            "move_today_pct": d.get("move_today_pct", b.get("move_today_pct")),
            "agent_run_at": agent.get("generated_at"), "agent_rank": d.get("rank"),
            "greens": d.get("greens", b.get("greens")), "agent_signal": d.get("signal", b.get("signal")),
            "check1_data_status": c1.get("status", b.get("price_check")), "check1_spread_pct": c1.get("spread_pct"), "check1_summary": c1.get("summary"),
            "check2_organic_status": c2.get("status", b.get("organic")), "check2_score": c2.get("score", b.get("organic_score")),
            "check2_flags": "; ".join(f.get("text", "") for f in c2.get("flags") or []) or None,
            "check3_prediction_status": c3.get("status", b.get("prediction")), "check3_call": c3.get("call"),
            "check3_target": c3.get("target"), "check3_low": c3.get("low"), "check3_high": c3.get("high"),
            "check3_due": c3.get("due"), "check3_confidence": c3.get("confidence"),
            "check4_news_status": c4.get("status", b.get("news")), "check4_headlines": c4.get("count"),
            "check4_top_headline": heads[0]["headline"] if heads else None,
            "position_qty_before": num(pos.get("qty")) or None, "position_avg_entry": num(pos.get("avg_entry_price")) or None,
            "position_plpc_before": round(num(pos.get("unrealized_plpc")) * 100, 2) if pos else None,
        }

    def place(sym, side, reason, notional=None, qty=None, rule=None, asset=None):
        body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": f"{TAG}{side}-{sym}-{int(time.time()*1000)}"[:48]}
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        act = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "symbol": sym, "side": side,
               "notional": body.get("notional"), "qty": body.get("qty"), "reason": reason}
        entry = context(sym, side, reason, rule, asset)
        entry.update(client_order_id=body["client_order_id"], order_type="market", time_in_force="day",
                     qty_requested=num(body.get("qty")) or None, notional_requested=num(body.get("notional")) or None,
                     decided_at_utc=act["time"])
        try:
            o = call("POST", "/v2/orders", body=body)
            act.update(status=o.get("status"), order_id=o.get("id"))
            entry.update(order_id=o.get("id"), status=o.get("status"), submitted_at_utc=o.get("submitted_at"),
                         filled_at_utc=o.get("filled_at"), filled_qty=num(o.get("filled_qty")) or None,
                         filled_avg_price=num(o.get("filled_avg_price")) or None)
        except Exception as e:
            act.update(status="rejected", error=str(e)[:200])
            entry.update(status="rejected", error=str(e)[:200])
        out["actions"].append(act)
        JOURNAL.append(entry)
        return act

    # ---- exits first (they free cash and slots)
    for sym in sorted(owned):
        if sym in open_syms:
            continue
        if sym in bought_today and pdt_limited:
            out["notes"].append(f"{sym}: exit waits until tomorrow, the account has used its 3 day trades (under $25k rule).")
            continue
        p = positions[sym]
        qty, plpc = num(p.get("qty")), num(p.get("unrealized_plpc")) * 100
        price, entry = num(p.get("current_price")), num(p.get("avg_entry_price"))
        day = num(p.get("change_today")) * 100
        peak = max(num(PEAKS.get(sym)), price, entry)
        PEAKS[sym] = peak
        peak_gain = (peak / entry - 1) * 100 if entry else 0
        drop_from_peak = (1 - price / peak) * 100 if peak else 0
        b = board.get(sym)
        greens = b["greens"] if b else None
        if plpc <= -STOP_LOSS_PCT:
            place(sym, "sell", f"Stop-loss: down {abs(plpc):.1f}% from entry", qty=qty, rule="stop-loss")
        elif peak_gain >= TRAIL_ARM_PCT and drop_from_peak >= TRAIL_PCT:
            place(sym, "sell", f"Trailing stop: was up {peak_gain:.1f}%, fell {drop_from_peak:.1f}% from its best", qty=qty, rule="trailing stop")
        elif day <= -BAD_DAY_PCT and greens != 4:
            place(sym, "sell", f"Bad day: down {abs(day):.1f}% today and only {greens if greens is not None else '?'} of 4 green", qty=qty, rule="bad day")
        elif plpc >= TAKE_PROFIT_PCT:
            place(sym, "sell", f"Take profit: up {plpc:.1f}% from entry", qty=qty, rule="take-profit")
        elif greens is not None and greens <= 2:
            place(sym, "sell", f"Only {greens} of 4 green", qty=qty, rule="2 or fewer green")
        elif greens == 3 and sym not in trimmed_recently:
            half = math.floor(qty / 2 * 1e9) / 1e9 if p.get("asset_class") == "crypto" or qty != int(qty) else math.floor(qty / 2)
            if half > 0:
                place(sym, "sell", "3 of 4 green: start selling (half)", qty=half, rule="3 green: sell half")

    # ---- buys
    if MODE != "on":
        out["notes"].append("Sell-only: exits run, no new buys.")
    elif closing:
        out["notes"].append(f"{round(minutes_left)} min to the close: no new buys, sells only.")
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
                act = place(sym, "buy", f"4 of 4 green, rank #{c.get('rank')}, one slot ({100/SLOTS:.1f}%)", notional=amount, rule="4 of 4 green", asset=asset)
            else:
                price = num(c.get("price"))
                shares = math.floor(amount / price) if price else 0
                if shares < 1:
                    out["notes"].append(f"Skipped {sym}: not fractionable and one share costs more than a slot.")
                    continue
                amount = shares * price
                act = place(sym, "buy", f"4 of 4 green, rank #{c.get('rank')}, {shares} whole share(s)", qty=shares, rule="4 of 4 green", asset=asset)
            if act.get("status") != "rejected":
                spend -= amount
                bought += 1
        if not candidates:
            out["notes"].append("No stock has 4 of 4 green right now. Nothing to buy.")
        elif bought == 0 and free <= 0:
            out["notes"].append(f"All {SLOTS} slots are full.")

    for sym in list(PEAKS):
        if sym not in owned:
            PEAKS.pop(sym)
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
    ap.add_argument("--journal")
    ap.add_argument("--journal-previous")
    ap.add_argument("--state", help="JSON file that keeps trailing-stop peaks between runs")
    a = ap.parse_args()
    if a.state:
        PEAKS.update(load(a.state).get("peaks", {}))
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
    if a.state:
        with open(a.state, "w") as f:
            json.dump({"updated_at": out.get("generated_at"), "peaks": PEAKS}, f, indent=1)
    if a.journal:
        prev = load(a.journal_previous).get("entries", [])
        entries = prev + JOURNAL
        if KEY and SECRET:
            try:
                known = {e.get("order_id") for e in entries}
                since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for o in reversed(call("GET", "/v2/orders", {"status": "all", "limit": 500, "direction": "desc", "after": since})):
                    if (o.get("client_order_id") or "").startswith(TAG) and o.get("id") not in known:
                        entries.append({"side": o.get("side"), "symbol": o.get("symbol"), "order_id": o.get("id"),
                                        "client_order_id": o.get("client_order_id"), "status": o.get("status"),
                                        "order_type": o.get("type"), "time_in_force": o.get("time_in_force"),
                                        "submitted_at_utc": o.get("submitted_at"), "decided_at_utc": o.get("submitted_at"),
                                        "qty_requested": num(o.get("qty")) or None, "notional_requested": num(o.get("notional")) or None,
                                        "mode": "paper" if PAPER else "live",
                                        "reason": "Recovered from Alpaca order history (details at decision time not available)"})
                entries.sort(key=lambda e: e.get("decided_at_utc") or e.get("submitted_at_utc") or "")
                refresh_journal(entries)
            except Exception as e:
                out.setdefault("notes", []).append(f"Journal refresh failed: {e}"[:200])
        for i, e in enumerate(entries, 1):
            e["journal_no"] = i
        with open(a.journal, "w") as f:
            json.dump({"generated_at": out.get("generated_at"), "entries": entries}, f, separators=(",", ":"))
    print(f"autotrader mode={out.get('mode')} actions={len(out.get('actions', []))} "
          f"notes={out.get('notes')} error={out.get('error')}")


if __name__ == "__main__":
    main()
