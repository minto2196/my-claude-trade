#!/usr/bin/env python3
"""Auto-trader for the Trade Desk: acts on the verification agent's flags in your Alpaca account.

THIS PLACES REAL ORDERS when ALPACA_PAPER is false. It does nothing unless you switch it on.

Kill switch: the AUTOTRADE repository variable (or secret).
  off        (default, also when unset or anything unrecognised)  no orders at all
  sell-only  no new buys; exits below still run
  on         buys and exits

Level 0 master switch: the weekly time machine (backtest.py) replays the last ~6 months with these
rules. If it would have lost money, or there is no valid result, no new buys (exits still run).

Buying (only while the US market is open):
  * only stocks the agent marks "buy": 4 or 5 of 5 green, time machine and data-is-real green,
    nothing red, and that you don't already hold
  * only operating companies (funds/ETFs skipped unless AUTOTRADE_ALLOW_ETF=true)
  * LOOP: each buy uses AUTOTRADE_SLOT_PCT (8.33%) of the cash available at that moment, then the
    next buy-ready stock gets 8.33% of what is left, and so on (cash only, never margin), until no
    stock is buy-ready, the order would be under AUTOTRADE_MIN_ORDER ($5), or AUTOTRADE_MAX_POSITIONS
    (40) are held. $1,000 -> $83.30, $76.36, $70.00, ... so more than 12 stocks are possible.
  * never buys back a stock it sold today
  * no buys on a day the account is down AUTOTRADE_DAILY_LOSS_PCT (default 5%) or more
  * no buys in the last AUTOTRADE_NO_BUY_MINUTES (default 60 with same-day selling, else 30) before the close

Exits (only on positions the auto-trader bought itself; your manual positions are never touched):
  * 2 or fewer of 5 green, or the stock's time machine turns red -> sell all
  * 3 of 5 green               -> sell half, once (not again within 3 days)
  * SELL LINE = 5% below the highest price since buying, never below 5% under the buy price
    (bought 100 -> line 95; rises to 200 -> line 190). Price at or under the line -> sell all.
    EVENING: in the last AUTOTRADE_FLATTEN_MINUTES (30) before the close, every position up less than
    AUTOTRADE_KEEP_OVERNIGHT_PCT (10%) from its buy price is sold; positions up 10%+ stay overnight
    and keep their sell line. No buys in the last 60 minutes. A position that closed under +10% but
    is still held at the next open (missed evening run) is sold at once. Take-profit is off.
  * The sell line is also kept as a REAL stop order at Alpaca: right after a
    buy fills, a sell-stop is placed on the line and raised every run as the price climbs, so Alpaca's own
    servers sell within seconds instead of waiting for the next GitHub run. Alpaca only accepts
    these as day orders for fractional shares, so they are renewed at the first run each day.
  * bad day: down BAD_DAY_PCT (3%) today and not 5 of 5 green -> sell all
  * up   TAKE_PROFIT_PCT -> sell all (off by default)
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
TRAIL_ARM_PCT = float(os.environ.get("AUTOTRADE_TRAIL_ARM_PCT", "0") or 0)
TRAIL_PCT = float(os.environ.get("AUTOTRADE_TRAIL_PCT", "5") or 5)
BAD_DAY_PCT = float(os.environ.get("AUTOTRADE_BAD_DAY_PCT", "3") or 3)
PEAKS = {}            # best price seen per owned symbol (trailing stop); loaded/saved via --state
GROUPS = {}           # risk group per owned symbol, this run
TAKE_PROFIT_PCT = float(os.environ.get("AUTOTRADE_TAKE_PROFIT_PCT", "0") or 0)   # 0 = off: let winners run
FLATTEN = os.environ.get("AUTOTRADE_FLATTEN", "true").strip().lower() != "false"   # sell everything before the close
FLATTEN_MINUTES = float(os.environ.get("AUTOTRADE_FLATTEN_MINUTES", "30") or 30)
KEEP_OVERNIGHT_PCT = float(os.environ.get("AUTOTRADE_KEEP_OVERNIGHT_PCT", "10") or 10)   # evening: keep only positions up this much
SLOT_PCT = float(os.environ.get("AUTOTRADE_SLOT_PCT", "8.33") or 8.33)                    # each buy = this % of the cash available then
MAX_POSITIONS = max(1, int(os.environ.get("AUTOTRADE_MAX_POSITIONS", "40") or 40))
MIN_ORDER = float(os.environ.get("AUTOTRADE_MIN_ORDER", "5") or 5)
BUY_MIN_GREENS = int(os.environ.get("AUTOTRADE_BUY_MIN_GREENS", "4") or 4)        # 5 = buy only 5/5 green
DAILY_LOSS_PCT = float(os.environ.get("AUTOTRADE_DAILY_LOSS_PCT", "5") or 5)
NO_BUY_MINUTES = float(os.environ.get("AUTOTRADE_NO_BUY_MINUTES", "60" if FLATTEN else "30") or 30)
ALLOW_ETF = os.environ.get("AUTOTRADE_ALLOW_ETF", "false").strip().lower() == "true"
MAX_AGENT_AGE_MIN = 30
TAG = "tdagent-"
STOP_TAG = TAG + "stop-"
WD_RULES = {"line": ("sell line (watchdog)", "price reached its sell line"),
            "signal": ("2 or fewer green (watchdog)", "high risk and the checks say sell"),
            "evening": ("evening under 10% (watchdog)", "evening rule, under +10% before the close"),
            "overnight": ("overnight catch-up (watchdog)", "held overnight under +10%, sold at the open")}
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


def fetch_orders(days):
    """All orders of the last `days` days, newest first (pages of 500; stop orders add many)."""
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, until = [], None
    for _ in range(12):
        p = {"status": "all", "limit": 500, "direction": "desc", "after": after}
        if until:
            p["until"] = until
        page = call("GET", "/v2/orders", p)
        out += [o for o in page if o.get("id") not in {x.get("id") for x in out[-500:]}]
        if len(page) < 500:
            break
        until = page[-1].get("submitted_at")
    return out


LINE_PCT = {"safe": float(os.environ.get("AUTOTRADE_LINE_SAFE_PCT", "5") or 5),
            "low": float(os.environ.get("AUTOTRADE_LINE_LOW_PCT", "4") or 4),
            "high": float(os.environ.get("AUTOTRADE_LINE_HIGH_PCT", "3") or 3)}
GROUP_LABEL = {"safe": "Safe", "low": "Low risk", "high": "High risk"}


def stop_line(entry, peak=None, pct=None):
    """The sell line: pct (5% safe, 4% low risk, 3% high risk) below the highest price since buying,
    never lower than STOP_LOSS_PCT under the buy price. Bought 100, high 200, safe -> 190."""
    pct = TRAIL_PCT if pct is None else pct
    px = entry * (1 - STOP_LOSS_PCT / 100)
    if peak is None:
        peak = entry
    if peak >= entry * (1 + TRAIL_ARM_PCT / 100):
        px = max(px, peak * (1 - pct / 100))
    return round(px, 2) if px >= 1 else round(px, 4)


def stop_price(entry, peak=None, pct=None):
    return stop_line(entry, peak, pct)


def risk_group(statuses, price, entry, peak, day_pct, vol_pct):
    """Sort a held stock into safe / low / high risk. statuses: dict check -> status (may be empty).
    Returns (group, reasons)."""
    st = {k: v for k, v in (statuses or {}).items() if v}
    greens = sum(1 for v in st.values() if v == "ok")
    plpc = (price / entry - 1) * 100 if entry else 0
    safe_line = stop_line(entry, peak, LINE_PCT["safe"]) if entry else 0
    cushion = (price / safe_line - 1) * 100 if safe_line else 99
    high = []
    if any(v == "alert" for v in st.values()):
        high.append("a red check: " + ", ".join(k for k, v in st.items() if v == "alert"))
    if st and greens <= 3:
        high.append(f"only {greens} of 5 green")
    if day_pct is not None and day_pct <= -2:
        high.append(f"down {abs(day_pct):.1f}% today")
    if vol_pct is not None and vol_pct >= 4:
        high.append(f"wild swings (±{vol_pct:.1f}% a day)")
    # Being close to the line is shown on the dashboard but doesn't change the group: a tighter line
    # there would sell the stock before its own line is reached.
    if high:
        return "high", high
    if st and greens == 5 and (vol_pct is None or vol_pct < 2.5) and plpc >= 0 and (day_pct is None or day_pct > -1):
        return "safe", [f"5 of 5 green, calm (±{vol_pct:.1f}% a day)" if vol_pct is not None else "5 of 5 green",
                        f"{plpc:+.1f}% from the buy price"]
    why = []
    if st and greens < 5:
        why.append(f"{greens} of 5 green")
    if vol_pct is not None and vol_pct >= 2.5:
        why.append(f"swings ±{vol_pct:.1f}% a day")
    if plpc < 0:
        why.append(f"{plpc:+.1f}% from the buy price")
    if day_pct is not None and day_pct <= -1:
        why.append(f"down {abs(day_pct):.1f}% today")
    return "low", why or ["not every check is clearly green yet"]


def group_inputs(sym, board, detail):
    """Statuses and daily swing for a symbol from the agent's latest results."""
    b, d = board.get(sym) or {}, detail.get(sym) or {}
    ch = d.get("checks") or {}
    st = {"time machine": b.get("history") or (ch.get("history") or {}).get("status"),
          "data": b.get("price_check") or (ch.get("price") or {}).get("status"),
          "organic": b.get("organic") or (ch.get("organic") or {}).get("status"),
          "prediction": b.get("prediction") or (ch.get("prediction") or {}).get("status"),
          "news": b.get("news") or (ch.get("news") or {}).get("status")}
    if st.get("news") == "skip":
        st["news"] = "watch"
    vol = ((ch.get("organic") or {}).get("stats") or {}).get("daily_vol_pct")
    return st, vol


def run(agent, previous, backtest=None):
    now = dt.datetime.now(dt.timezone.utc)
    out = {"generated_at": now.isoformat(timespec="seconds"), "mode": MODE, "paper": PAPER,
           "rules": {"slots": SLOTS, "slot_pct": SLOT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
                     "trail_arm_pct": TRAIL_ARM_PCT, "trail_pct": TRAIL_PCT, "bad_day_pct": BAD_DAY_PCT,
                     "take_profit_pct": TAKE_PROFIT_PCT, "daily_loss_pct": DAILY_LOSS_PCT, "allow_etf": ALLOW_ETF,
                     "no_buy_minutes_before_close": NO_BUY_MINUTES,
                     "sell_all_before_close": FLATTEN, "sell_all_minutes_before_close": FLATTEN_MINUTES,
                     "keep_overnight_pct": KEEP_OVERNIGHT_PCT, "buy_pct_of_cash": SLOT_PCT, "max_positions": MAX_POSITIONS,
                     "min_order": MIN_ORDER, "line_pct": LINE_PCT, "buy_min_greens": BUY_MIN_GREENS},
           "actions": [], "notes": []}
    history = previous.get("history", [])
    if MODE == "off":
        out["notes"].append("Switched off. No orders placed. Set AUTOTRADE to on (or sell-only) to start.")
        out["history"] = history
        return out

    account = call("GET", "/v2/account")
    clock = call("GET", "/v2/clock")
    positions = {p["symbol"]: p for p in call("GET", "/v2/positions")}
    orders = fetch_orders(180)
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
    flatten_now = FLATTEN and minutes_left is not None and minutes_left <= FLATTEN_MINUTES
    out["minutes_to_close"] = round(minutes_left) if minutes_left is not None else None
    pdt_limited = (not PAPER and equity < 25000 and int(num(account.get("daytrade_count"))) >= 3)

    today = et_today()
    is_stop = lambda o: (o.get("client_order_id") or "").startswith(STOP_TAG)
    mine = [o for o in orders if (o.get("client_order_id") or "").startswith(TAG)]
    owned = {o["symbol"] for o in mine if o.get("side") == "buy" and o.get("status") in ("filled", "partially_filled")}
    owned &= set(positions)
    OPEN = ("new", "accepted", "pending_new", "partially_filled", "held")
    open_syms = {o["symbol"] for o in orders if o.get("status") in OPEN and not is_stop(o)}
    open_stops = {}
    for o in orders:
        if o.get("status") in OPEN and is_stop(o):
            open_stops.setdefault(o["symbol"], []).append(o)
    bought_today = {o["symbol"] for o in mine if o.get("side") == "buy" and (o.get("filled_at") or "")[:10] == today}
    sold_today = {o["symbol"] for o in mine if o.get("side") == "sell" and (o.get("filled_at") or o.get("submitted_at") or "")[:10] == today}
    trimmed_recently = {o["symbol"] for o in mine if o.get("side") == "sell" and o.get("status") == "filled" and not is_stop(o)
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
            "account_equity_before": equity, "account_cash_before": cash, "slot_pct": SLOT_PCT,
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
            "check5_history_status": (ch.get("history") or {}).get("status", b.get("history")),
            "check5_hit_rate": (ch.get("history") or {}).get("hit_rate", b.get("tm_hit_rate")),
            "check5_signals": (ch.get("history") or {}).get("n", b.get("tm_n")),
            "check5_avg_fwd_pct": (ch.get("history") or {}).get("avg_fwd_pct"),
            "check5_summary": (ch.get("history") or {}).get("summary"),
            "master_switch_return_pct": (backtest or {}).get("total_return_pct"),
            "position_qty_before": num(pos.get("qty")) or None, "position_avg_entry": num(pos.get("avg_entry_price")) or None,
            "position_plpc_before": round(num(pos.get("unrealized_plpc")) * 100, 2) if pos else None,
        }

    def cancel_stops(sym):
        """A stop order reserves the shares; cancel it before selling or replacing it."""
        ids = [o["id"] for o in open_stops.pop(sym, [])]
        for oid in ids:
            try:
                call("DELETE", f"/v2/orders/{oid}")
            except Exception:
                pass
        for oid in ids:
            for _ in range(10):
                try:
                    if call("GET", f"/v2/orders/{oid}").get("status") in ("canceled", "filled", "expired", "rejected"):
                        break
                except Exception:
                    break
                time.sleep(0.5)
        return len(ids)

    def place(sym, side, reason, notional=None, qty=None, rule=None, asset=None):
        if side == "sell":
            cancel_stops(sym)
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
        if sym in bought_today and pdt_limited and not flatten_now:
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
        sig = b.get("signal") if b else None
        st_in, vol = group_inputs(sym, board, detail)
        group, why = risk_group(st_in, price, entry, peak, day, vol)
        line = stop_line(entry, peak, LINE_PCT[group]) if entry else None
        GROUPS[sym] = {"symbol": sym, "group": group, "label": GROUP_LABEL[group], "reasons": why, "price": price,
                       "entry": round(entry, 4), "highest": round(peak, 4), "line": line, "line_pct": LINE_PCT[group],
                       "plpc": round(plpc, 2), "day_pct": round(day, 2), "to_line_pct": round((price / line - 1) * 100, 2) if line else None}
        trailing = line is not None and line > entry * (1 - STOP_LOSS_PCT / 100) + 1e-9
        last_close = num(p.get("lastday_price"))
        closed_pl = (last_close / entry - 1) * 100 if last_close and entry else None
        if flatten_now and plpc < KEEP_OVERNIGHT_PCT:
            place(sym, "sell", f"Evening: {plpc:+.1f}% from the buy price (under +{KEEP_OVERNIGHT_PCT:g}%), sold "
                               f"{round(minutes_left)} min before the close", qty=qty, rule="evening under 10%")
        elif flatten_now:
            out["notes"].append(f"{sym} is up {plpc:+.1f}% (+{KEEP_OVERNIGHT_PCT:g}% or more): kept overnight with its sell line at ${line:,.2f}.")
        elif (FLATTEN and sym not in bought_today and closed_pl is not None and closed_pl < KEEP_OVERNIGHT_PCT
              and not (line is not None and price > line and plpc >= KEEP_OVERNIGHT_PCT)):
            place(sym, "sell", f"Held overnight at {closed_pl:+.1f}% although the evening rule sells under +{KEEP_OVERNIGHT_PCT:g}% "
                               f"(the evening run was missed): sold at the first run, now {plpc:+.1f}%", qty=qty, rule="overnight catch-up")
        elif line is not None and price <= line and trailing:
            place(sym, "sell", f"{GROUP_LABEL[group]} sell line ({LINE_PCT[group]:g}%): highest ${peak:,.2f}, fell {drop_from_peak:.1f}% to ${price:,.2f} "
                               f"(line ${line:,.2f}), {plpc:+.1f}% from the buy price", qty=qty, rule="trailing stop")
        elif line is not None and price <= line:
            place(sym, "sell", f"{GROUP_LABEL[group]} sell line: down {abs(plpc):.1f}% from the buy price (line ${line:,.2f})", qty=qty, rule="stop-loss")
        elif day <= -BAD_DAY_PCT and greens != 5:
            place(sym, "sell", f"Bad day: down {abs(day):.1f}% today and only {greens if greens is not None else '?'} of 5 green", qty=qty, rule="bad day")
        elif TAKE_PROFIT_PCT > 0 and plpc >= TAKE_PROFIT_PCT:
            place(sym, "sell", f"Take profit: up {plpc:.1f}% from entry", qty=qty, rule="take-profit")
        elif b and b.get("history") == "alert":
            place(sym, "sell", "Time machine turned red: this stock usually fell after looking green", qty=qty, rule="time machine red")
        elif sig == "sell":
            place(sym, "sell", f"Only {greens} of 5 green", qty=qty, rule="2 or fewer green")
        elif sig == "start_selling" and sym not in trimmed_recently:
            half = math.floor(qty / 2 * 1e9) / 1e9 if p.get("asset_class") == "crypto" or qty != int(qty) else math.floor(qty / 2)
            if half > 0:
                place(sym, "sell", "3 of 5 green: start selling (half)", qty=half, rule="3 green: sell half")

    # ---- buys
    bt = backtest or {}
    bt_age = None
    try:
        bt_age = (now - dt.datetime.fromisoformat(bt["generated_at"])).days
    except Exception:
        pass
    out["master_switch"] = {"buys_allowed": bool(bt.get("buys_allowed")) and bt_age is not None and bt_age <= 10,
                            "total_return_pct": bt.get("total_return_pct"), "generated_at": bt.get("generated_at")}
    if MODE != "on":
        out["notes"].append("Sell-only: exits run, no new buys.")
    elif not out["master_switch"]["buys_allowed"]:
        why = ("there is no recent time-machine result" if bt_age is None or bt_age > 10 or bt.get("buys_allowed") is None
               else f"the time machine says these rules would have returned {bt.get('total_return_pct')}% over the last 6 months")
        out["notes"].append(f"Master switch: buying paused because {why}. Exits still run.")
    elif closing:
        out["notes"].append(f"{round(minutes_left)} min to the close: no new buys, sells only.")
    elif last_equity and (equity / last_equity - 1) * 100 <= -DAILY_LOSS_PCT:
        out["notes"].append(f"Account down {(equity/last_equity-1)*100:.1f}% today. No new buys until tomorrow.")
    else:
        free = MAX_POSITIONS - len(owned)
        if FLATTEN and not PAPER and equity < 25000:
            budget = max(0, 3 - int(num(account.get("daytrade_count"))) - len(bought_today))
            if budget < free:
                out["notes"].append(f"Live account under $25,000: selling the same day makes every buy a day trade, and Alpaca allows "
                                    f"3 per 5 days. Buying at most {budget} today.")
            free = min(free, budget)
        spend = cash
        candidates = [s for s in agent.get("symbols", []) if s.get("signal") == "buy" and (s.get("greens") or 0) >= BUY_MIN_GREENS]
        candidates.sort(key=lambda s: s.get("rank") or 999)
        bought = 0
        for c in candidates:
            if bought >= free:
                break
            sym = c["symbol"]
            if sym in positions or sym in open_syms or sym in sold_today or board.get(sym, {}).get("signal") != "buy":
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
            amount = math.floor(spend * SLOT_PCT / 100 * 100) / 100
            if amount < MIN_ORDER:
                out["notes"].append(f"Next order would be ${amount:.2f} ({SLOT_PCT:g}% of ${spend:,.2f} cash), under the ${MIN_ORDER:g} minimum. Stopped buying.")
                break
            if asset.get("fractionable"):
                act = place(sym, "buy", f"{c.get('greens')} of 5 green, time machine green, rank #{c.get('rank')}, "
                                        f"{SLOT_PCT:g}% of ${spend:,.2f} cash", notional=amount, rule="4+/5 green", asset=asset)
            else:
                price = num(c.get("price"))
                shares = math.floor(amount / price) if price else 0
                if shares < 1:
                    out["notes"].append(f"Skipped {sym}: not fractionable and one share costs more than a slot.")
                    continue
                amount = shares * price
                act = place(sym, "buy", f"{c.get('greens')} of 5 green, time machine green, rank #{c.get('rank')}, {shares} whole share(s)", qty=shares, rule="4+/5 green", asset=asset)
            if act.get("status") != "rejected":
                spend -= amount
                bought += 1
        if not candidates:
            out["notes"].append("No stock is buy-ready right now. Nothing to buy.")
        elif bought == 0 and free <= 0:
            out["notes"].append(f"Holding the maximum of {MAX_POSITIONS} stocks.")

    protect = set()
    # ---- protective stop orders at Alpaca: exactly STOP_LOSS_PCT below the average buy price
    time.sleep(3)   # give this run's market orders a moment to fill
    try:
        pos_now = {p["symbol"]: p for p in call("GET", "/v2/positions")}
        recent = call("GET", "/v2/orders", {"status": "all", "limit": 200, "direction": "desc",
                                             "after": (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        just_bought = {o["symbol"] for o in recent if (o.get("client_order_id") or "").startswith(TAG + "buy-")
                       and o.get("status") in ("filled", "partially_filled")}
        busy = {o["symbol"] for o in recent if o.get("status") in OPEN and not is_stop(o)}
        protect = (owned | just_bought) & set(pos_now)
        stops = []
        for sym in sorted(protect):
            p = pos_now[sym]
            if p.get("asset_class") == "crypto" or sym in busy:
                continue
            qty, entry, price = num(p.get("qty")), num(p.get("avg_entry_price")), num(p.get("current_price"))
            if qty <= 0 or not entry:
                continue
            PEAKS[sym] = max(num(PEAKS.get(sym)), price, entry)
            sp = stop_price(entry, PEAKS[sym])
            g = GROUPS.get(sym, {}).get("group") or "low"
            sp = stop_price(entry, PEAKS[sym], LINE_PCT[g])
            if price and price <= sp:
                continue   # already below the line; the exit rules above handle it
            if flatten_now:
                continue   # being sold before the close
            have = open_stops.get(sym, [])
            if len(have) == 1 and abs(num(have[0].get("qty")) - qty) < 1e-6 and num(have[0].get("stop_price")) >= sp - 0.005:
                sp = num(have[0].get("stop_price"))   # never lower a stop (the every-minute watchdog may have raised it)
                stops.append(f"{sym} {sp:g}")
                continue
            if pdt_limited and sym in (bought_today | just_bought):
                out["notes"].append(f"{sym}: Alpaca stop order waits until tomorrow (3 day trades used, under $25k rule).")
                continue
            cancel_stops(sym)
            body = {"symbol": sym, "side": "sell", "type": "stop", "stop_price": f"{sp}", "time_in_force": "day",
                    "qty": f"{qty:.9f}".rstrip("0").rstrip("."), "client_order_id": f"{STOP_TAG}{sym}-{int(time.time()*1000)}"[:48]}
            try:
                o = call("POST", "/v2/orders", body=body)
                stops.append(f"{sym} {sp:g}")
                out.setdefault("stop_orders", []).append({"symbol": sym, "stop_price": sp, "entry": round(entry, 4),
                                                          "highest": round(PEAKS[sym], 4), "qty": qty, "status": o.get("status")})
            except Exception as e:
                out["notes"].append(f"Alpaca refused the stop order for {sym}: {str(e)[:120]}. The 5-minute check still applies.")
        for sym in list(open_stops):          # stops left on positions that are gone
            if sym not in pos_now:
                cancel_stops(sym)
        out["protective_stops"] = stops
        if stops:
            out["notes"].append(f"Alpaca stop orders on each stock's sell line (safe 5%, low risk 4%, high risk 3% below the highest price) in place: {', '.join(stops)}.")
    except Exception as e:
        out["notes"].append(f"Couldn't check the Alpaca stop orders: {str(e)[:150]}")

    for sym in list(PEAKS):
        if sym not in owned and sym not in protect:
            PEAKS.pop(sym)
    out["owned"] = sorted(owned)
    out["groups"] = sorted(GROUPS.values(), key=lambda g: ({"high": 0, "low": 1, "safe": 2}[g["group"]], g["symbol"]))
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
    ap.add_argument("--backtest", help="the weekly time machine result (master switch)")
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
            out = run(agent, previous, load(a.backtest))
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
                for o in reversed(fetch_orders(365)):
                    cid = o.get("client_order_id") or ""
                    if cid.startswith(STOP_TAG) and o.get("status") not in ("filled", "partially_filled"):
                        continue   # renewed every day; only a triggered stop is a trade
                    if cid.startswith(TAG) and o.get("id") not in known:
                        entries.append({"side": o.get("side"), "symbol": o.get("symbol"), "order_id": o.get("id"),
                                        "client_order_id": o.get("client_order_id"), "status": o.get("status"),
                                        "order_type": o.get("type"), "time_in_force": o.get("time_in_force"),
                                        "submitted_at_utc": o.get("submitted_at"), "decided_at_utc": o.get("submitted_at"),
                                        "qty_requested": num(o.get("qty")) or None, "notional_requested": num(o.get("notional")) or None,
                                        "mode": "paper" if PAPER else "live",
                                        "rule": ("stop-loss (Alpaca order)" if cid.startswith(STOP_TAG) else
                                                 WD_RULES.get(cid[len(TAG) + 3:].split("-")[0], ("watchdog", ""))[0] if cid.startswith(TAG + "wd-") else None),
                                        "reason": (f"Alpaca stop order triggered at {o.get('stop_price')} (the stock's sell line)"
                                                   if cid.startswith(STOP_TAG) else
                                                   "Every-minute watchdog: " + WD_RULES.get(cid[len(TAG) + 3:].split("-")[0], ("", "sold"))[1]
                                                   if cid.startswith(TAG + "wd-") else
                                                   "Recovered from Alpaca order history (details at decision time not available)")})
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
