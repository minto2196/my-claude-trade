#!/usr/bin/env python3
"""Level 0 time machine: replays the whole strategy over the last SIM_DAYS trading days.

Every Saturday from 10:00 UTC (and whenever the last result is missing or failed) it goes back
~6 months and trades day by day with the same rules the auto-trader uses, seeing only what was
known on each day:

  * the 500 stocks the agent shortlisted today (see caveats)
  * checks replayed on each past day: data is real (assumed green), organic trend, prediction,
    news (assumed quiet = green; old headlines aren't replayed), time machine (walk-forward:
    only outcomes already known that day)
  * buys at the next day's open when 4+/5 green, vetoes green, no red; 12 equal slots, cash only
  * exits: stop-loss, trailing stop, take profit (intraday, against the day's low/high),
    bad day at the close, signal exits (3/5 sell half, 2/5 or time machine red sell all) next open
  * 0.05% slippage per trade, no buys after a -5% day

MASTER SWITCH: if the replay would have LOST money, buys_allowed is false and the auto-trader
goes sell-only until a later replay turns positive.

Usage: python3 backtest.py RECORDS_DIR AGENT_JSON SITE_DATA_DIR [--force] [--now ISO]
"""
import datetime as dt
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_check as A  # noqa: E402

UTC = dt.timezone.utc
SIM_DAYS = int(os.environ.get("BACKTEST_DAYS", "126"))
START_CASH = 100_000.0
SLOTS = int(os.environ.get("AUTOTRADE_SLOTS", "12") or 12)
STOP = float(os.environ.get("AUTOTRADE_STOP_LOSS_PCT", "5") or 5) / 100
TRAIL_ARM = float(os.environ.get("AUTOTRADE_TRAIL_ARM_PCT", "4") or 4) / 100
TRAIL = float(os.environ.get("AUTOTRADE_TRAIL_PCT", "4") or 4) / 100
TAKE = float(os.environ.get("AUTOTRADE_TAKE_PROFIT_PCT", "20") or 20) / 100
BAD_DAY = float(os.environ.get("AUTOTRADE_BAD_DAY_PCT", "3") or 3) / 100
DAILY_LOSS = float(os.environ.get("AUTOTRADE_DAILY_LOSS_PCT", "5") or 5) / 100
SLIP = 0.0005
REPORT_HOUR = 10


def last_due(now):
    d = now.date() - dt.timedelta(days=(now.weekday() - 5) % 7)
    due = dt.datetime.combine(d, dt.time(REPORT_HOUR), tzinfo=UTC)
    return due if due <= now else due - dt.timedelta(days=7)


def is_due(latest, now):
    if not latest:
        return True
    gen = A.dt.datetime.fromisoformat(latest.get("generated_at", "1970-01-01T00:00:00+00:00"))
    if latest.get("error"):
        return (now - gen).total_seconds() > 3600
    return gen < last_due(now)


class History:
    """Walk-forward check 5 for one stock: prefix sums over past green days with known outcomes."""

    def __init__(self, ser):
        self.n, self.hits, self.sum = [0], [0], [0.0]
        for _, green, fwd, _, _ in ser:
            ok = green and fwd is not None
            self.n.append(self.n[-1] + ok)
            self.hits.append(self.hits[-1] + (ok and fwd > 0))
            self.sum.append(self.sum[-1] + (fwd if ok else 0.0))

    def status(self, k):
        """Status on bar k (index into ser): green days i in [k-LOOKBACK, k-HORIZON]."""
        hi = k - A.HORIZON + 1
        lo = max(0, k - A.TM_LOOKBACK)
        if hi <= lo:
            return "watch", None
        n = self.n[hi] - self.n[lo]
        if n < A.TM_MIN_SIGNALS:
            return "watch", None
        hit = (self.hits[hi] - self.hits[lo]) / n
        avg = (self.sum[hi] - self.sum[lo]) / n
        if hit >= A.TM_HIT_OK and avg > 0:
            return "ok", hit
        if avg < 0 and hit < 0.5:
            return "alert", hit
        return "watch", hit


def simulate(daily, spy, sim_days):
    t0 = time.time()
    dates = [b["t"][:10] for b in spy][-sim_days:]
    if len(dates) < 20:
        raise RuntimeError("Not enough market days to replay.")
    data = {}
    for s, bars in daily.items():
        if len(bars) < 120:
            continue
        ser = A.replay_series(bars, False, 60)
        idx = {b["t"][:10]: i for i, b in enumerate(bars)}
        data[s] = {"bars": bars, "ser": ser, "idx": idx, "hist": History(ser)}

    def statuses(s, day):
        d = data[s]
        i = d["idx"].get(day)
        if i is None or i < 60:
            return None
        k = i - 60
        _, _, _, org, call = d["ser"][k]
        h, hit = d["hist"].status(k)
        pred = {"up": "ok", "flat": "watch", "down": "alert"}.get(call, "watch")
        return ["ok", org, pred, "ok", h], hit

    cash, pos, pending, trades, curve = START_CASH, {}, [], [], []
    prev_equity = START_CASH
    spy0 = None
    spy_by = {b["t"][:10]: b for b in spy}

    def bar(s, day):
        i = data[s]["idx"].get(day)
        return data[s]["bars"][i] if i is not None else None

    def sell(s, qty, px, day, rule):
        nonlocal cash
        p = pos[s]
        px *= 1 - SLIP
        cash += qty * px
        trades.append({"symbol": s, "bought": p["day"], "buy_px": round(p["entry"], 4), "sold": day, "sell_px": round(px, 4),
                       "qty": round(qty, 6), "pl": round(qty * (px - p["entry"]), 2), "pl_pct": round((px / p["entry"] - 1) * 100, 2),
                       "days": (dt.date.fromisoformat(day) - dt.date.fromisoformat(p["day"])).days, "rule": rule})
        p["qty"] -= qty
        if p["qty"] <= 1e-9:
            pos.pop(s)

    for di, day in enumerate(dates):
        # a. orders decided at yesterday's close fill at today's open
        for kind, s, val, rule in pending:
            b = bar(s, day)
            if not b:
                continue
            if kind == "buy" and s not in pos and len(pos) < SLOTS:
                px = b["o"] * (1 + SLIP)
                amt = min(val, cash)
                if amt >= 1:
                    cash -= amt
                    pos[s] = {"qty": amt / px, "entry": px, "peak": px, "day": day, "trim": None}
            elif kind == "sell" and s in pos:
                sell(s, pos[s]["qty"] * val, b["o"], day, rule)
                if val < 1 and s in pos:
                    pos[s]["trim"] = di
        pending = []
        # b. intraday protective exits
        for s in list(pos):
            b = bar(s, day)
            if not b:
                continue
            p = pos[s]
            if b["l"] <= p["entry"] * (1 - STOP):
                sell(s, p["qty"], min(b["o"], p["entry"] * (1 - STOP)), day, "stop-loss")
            elif p["peak"] >= p["entry"] * (1 + TRAIL_ARM) and b["l"] <= p["peak"] * (1 - TRAIL):
                sell(s, p["qty"], min(b["o"], p["peak"] * (1 - TRAIL)), day, "trailing stop")
            elif b["h"] >= p["entry"] * (1 + TAKE):
                sell(s, p["qty"], max(b["o"], p["entry"] * (1 + TAKE)), day, "take-profit")
            else:
                p["peak"] = max(p["peak"], b["h"])
        # c. close: bad day + signal exits (filled next open)
        for s in list(pos):
            b, i = bar(s, day), data[s]["idx"].get(day)
            st = statuses(s, day)
            if not b or not st or not i:
                continue
            sts, _ = st
            greens = sts.count("ok")
            prevc = data[s]["bars"][i - 1]["c"]
            if b["c"] <= prevc * (1 - BAD_DAY) and greens < 5:
                sell(s, pos[s]["qty"], b["c"], day, "bad day")
                continue
            sig = A.signal(sts, True)
            if sig == "sell":
                pending.append(("sell", s, 1.0, "time machine red" if sts[4] == "alert" else "2 or fewer green"))
            elif sig == "start_selling" and (pos[s]["trim"] is None or di - pos[s]["trim"] >= 3):
                pending.append(("sell", s, 0.5, "3 green: sell half"))
        # d. equity, then buys for tomorrow's open
        equity = cash + sum(p["qty"] * (bar(s, day) or {"c": p["entry"]})["c"] for s, p in pos.items())
        free = SLOTS - len(pos)
        if free > 0 and equity > prev_equity * (1 - DAILY_LOSS):
            cands = []
            for s in data:
                if s in pos:
                    continue
                st = statuses(s, day)
                if st and A.signal(st[0], False) == "buy":
                    cands.append((st[1] or 0, st[0].count("ok"), s))
            cands.sort(reverse=True)
            budget = cash
            for _, _, s in cands[:free]:
                amt = min(equity / SLOTS, budget)
                if amt < 1:
                    break
                pending.append(("buy", s, amt, "4+/5 green"))
                budget -= amt
        prev_equity = equity
        sb = spy_by.get(day)
        if sb:
            spy0 = spy0 or sb["c"]
        curve.append([day, round(equity, 2), round(START_CASH * sb["c"] / spy0, 2) if sb and spy0 else None,
                      round(sum(p["qty"] * (bar(s, day) or {"c": p["entry"]})["c"] for s, p in pos.items()) / equity * 100, 1) if equity else 0])

    end = curve[-1][1]
    peak, mdd = START_CASH, 0.0
    for _, e, _, _ in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    closed = trades
    wins = [t for t in closed if t["pl"] > 0]
    by_rule = {}
    for t in closed:
        by_rule[t["rule"]] = by_rule.get(t["rule"], 0) + 1
    spy_ret = (curve[-1][2] / START_CASH - 1) * 100 if curve[-1][2] else None
    ret = (end / START_CASH - 1) * 100
    return {
        "period": {"from": dates[0], "to": dates[-1], "trading_days": len(dates)},
        "universe": len(data),
        "start_cash": START_CASH, "end_equity": round(end, 2),
        "total_return_pct": round(ret, 2), "spy_return_pct": round(spy_ret, 2) if spy_ret is not None else None,
        "beat_spy": spy_ret is not None and ret > spy_ret,
        "max_drawdown_pct": round(mdd * 100, 2),
        "trades_closed": len(closed), "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_trade_pct": round(sum(t["pl_pct"] for t in closed) / len(closed), 2) if closed else None,
        "avg_days_held": round(sum(t["days"] for t in closed) / len(closed), 1) if closed else None,
        "exits_by_rule": by_rule, "open_positions_at_end": len(pos),
        "avg_invested_pct": round(sum(c[3] for c in curve) / len(curve), 1),
        "best_trade": max(closed, key=lambda t: t["pl"]) if closed else None,
        "worst_trade": min(closed, key=lambda t: t["pl"]) if closed else None,
        "curve": curve, "trades": closed[-400:], "seconds": round(time.time() - t0, 1),
    }


def main():
    rec, agent_p, site = sys.argv[1:4]
    now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]) if "--now" in sys.argv else dt.datetime.now(UTC)
    latest_p = os.path.join(rec, "state", "backtest_latest.json")
    try:
        latest = json.load(open(latest_p))
    except Exception:
        latest = None
    os.makedirs(site, exist_ok=True)
    if "--force" not in sys.argv and not is_due(latest, now):
        json.dump(latest, open(os.path.join(site, "backtest.json"), "w"), separators=(",", ":"))
        print(f"backtest: not due (last {latest.get('generated_at')}, buys_allowed={latest.get('buys_allowed')})")
        return
    out = {"generated_at": now.isoformat(timespec="seconds"), "next_due": (last_due(now) + dt.timedelta(days=7)).isoformat(),
           "rules": {"slots": SLOTS, "stop_loss_pct": STOP * 100, "trail_arm_pct": TRAIL_ARM * 100, "trail_pct": TRAIL * 100,
                     "take_profit_pct": TAKE * 100, "bad_day_pct": BAD_DAY * 100, "slippage_pct": SLIP * 100,
                     "tm_lookback_days": A.TM_LOOKBACK},
           "caveats": ["Tested on today's 500 most-traded stocks, so stocks that faded away are missing (results look a bit better than reality).",
                       "Past headlines aren't replayed: news is assumed quiet. Data is assumed real.",
                       "Trades are simulated at daily open/high/low/close prices; real runs happen every ~5 minutes."]}
    try:
        agent = json.load(open(agent_p))
        cols = agent.get("board_columns", [])
        syms = [r[cols.index("symbol")] for r in agent.get("board", []) if "/" not in r[cols.index("symbol")]
                and not r[cols.index("symbol")].endswith("USD")]
        if len(syms) < 50:
            raise RuntimeError(f"Only {len(syms)} shortlisted stocks available; the scan must run first.")
        days = int((SIM_DAYS + A.TM_LOOKBACK + 110) * 1.47)
        daily = A.bars(syms, False, "1Day", days, "Backtest", per=25)
        spy = A.bars(["SPY"], False, "1Day", days, "Backtest").get("SPY", [])
        res = simulate(daily, spy, SIM_DAYS)
        out.update(res)
        out["buys_allowed"] = res["total_return_pct"] >= 0
        out["verdict"] = ("PASS: the rules would have made money" if out["buys_allowed"]
                          else "FAIL: the rules would have lost money, so buying is paused")
    except Exception as e:
        out.update(error=f"{type(e).__name__}: {e}"[:300])
        if latest and not latest.get("error"):
            out["buys_allowed"] = latest.get("buys_allowed")
            out["verdict"] = "Replay failed this time; the previous result still applies."
            for k in ("total_return_pct", "spy_return_pct", "max_drawdown_pct", "period", "curve"):
                out.setdefault(k, latest.get(k))
        else:
            out["buys_allowed"] = False
            out["verdict"] = "No replay result yet, so buying is paused until one succeeds."
    os.makedirs(os.path.join(rec, "state"), exist_ok=True)
    os.makedirs(os.path.join(rec, "backtest"), exist_ok=True)
    json.dump(out, open(latest_p, "w"), separators=(",", ":"))
    if not out.get("error"):
        json.dump(out, open(os.path.join(rec, "backtest", f"backtest_{now.date().isoformat()}.json"), "w"), separators=(",", ":"))
    slim = {k: v for k, v in out.items() if k != "trades"}
    slim["trades"] = (out.get("trades") or [])[-60:]
    json.dump(slim, open(os.path.join(site, "backtest.json"), "w"), separators=(",", ":"))
    print(f"backtest: return={out.get('total_return_pct')}% spy={out.get('spy_return_pct')}% mdd={out.get('max_drawdown_pct')}% "
          f"trades={out.get('trades_closed')} buys_allowed={out.get('buys_allowed')} error={out.get('error')}")


if __name__ == "__main__":
    main()
