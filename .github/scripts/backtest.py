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
  * buys at the open: each one 8.33% of the cash still available (loop, up to 12 stocks)
  * sell line 5% under the highest price, never under -5% from the buy (checked against each day's
    low/high; with daily prices the order of the high and low is unknown, so a new high followed by
    a close under the raised line counts as sold on the line)
  * evening: everything under +5% from its buy price is sold at the close; +5% or more is kept
  * bad day and signal exits (3/5 half, 2/5 or red time machine all) at the close
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
import autotrader as AT  # noqa: E402  (risk groups and their sell lines)

UTC = dt.timezone.utc
SIM_DAYS = int(os.environ.get("BACKTEST_DAYS", "126"))
START_CASH = 100_000.0
SLOTS = int(os.environ.get("AUTOTRADE_SLOTS", "12") or 12)
STOP = float(os.environ.get("AUTOTRADE_STOP_LOSS_PCT", "5") or 5) / 100
TRAIL_ARM = float(os.environ.get("AUTOTRADE_TRAIL_ARM_PCT", "0") or 0) / 100
TRAIL = float(os.environ.get("AUTOTRADE_TRAIL_PCT", "5") or 5) / 100
TAKE = float(os.environ.get("AUTOTRADE_TAKE_PROFIT_PCT", "0") or 0) / 100      # 0 = off
FLATTEN = os.environ.get("AUTOTRADE_FLATTEN", "true").strip().lower() != "false"
KEEP = float(os.environ.get("AUTOTRADE_KEEP_OVERNIGHT_PCT", "5") or 5) / 100
SLOT_PCT = float(os.environ.get("AUTOTRADE_SLOT_PCT", "8.33") or 8.33) / 100
MAX_POS = max(1, int(os.environ.get("AUTOTRADE_MAX_POSITIONS", "100") or 100))
BUY_MIN_GREENS = int(os.environ.get("AUTOTRADE_BUY_MIN_GREENS", "4") or 4)
BAD_DAY = float(os.environ.get("AUTOTRADE_BAD_DAY_PCT", "3") or 3) / 100
DAILY_LOSS = float(os.environ.get("AUTOTRADE_DAILY_LOSS_PCT", "5") or 5) / 100
SLIP = 0.0005
REPORT_HOUR = 10


def last_due(now):
    d = now.date() - dt.timedelta(days=(now.weekday() - 5) % 7)
    due = dt.datetime.combine(d, dt.time(REPORT_HOUR), tzinfo=UTC)
    return due if due <= now else due - dt.timedelta(days=7)


def current_rules():
    return {"slots": SLOTS, "stop_loss_pct": STOP * 100, "trail_arm_pct": TRAIL_ARM * 100, "trail_pct": TRAIL * 100,
            "take_profit_pct": TAKE * 100, "bad_day_pct": BAD_DAY * 100, "slippage_pct": SLIP * 100,
            "tm_lookback_days": A.TM_LOOKBACK, "sell_all_before_close": FLATTEN, "keep_overnight_pct": KEEP * 100,
            "buy_pct_of_cash": SLOT_PCT * 100, "slot_of": AT.SLOT_OF, "max_positions": MAX_POS, "line_pct": AT.LINE_PCT,
            "buy_min_greens": BUY_MIN_GREENS, "buy_needs_prediction": A.BUY_NEEDS_PREDICTION}


def is_due(latest, now):
    if not latest:
        return True
    if latest.get("rules") != current_rules():
        return True   # the rules changed: replay them now instead of waiting for Saturday
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
    data, dates, statuses, spy_by, t0 = prepare(daily, spy, sim_days)
    return run_sim(data, dates, statuses, spy_by, t0)


def prepare(daily, spy, sim_days):
    """The slow part (replaying every stock's checks for every day), done once and reusable."""
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

    return data, dates, statuses, {b['t'][:10]: b for b in spy}, t0


def summarize(curve, trades, open_end, dates, universe, t0):
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
        "universe": universe,
        "start_cash": START_CASH, "end_equity": round(end, 2),
        "total_return_pct": round(ret, 2), "spy_return_pct": round(spy_ret, 2) if spy_ret is not None else None,
        "beat_spy": spy_ret is not None and ret > spy_ret,
        "max_drawdown_pct": round(mdd * 100, 2),
        "trades_closed": len(closed), "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_trade_pct": round(sum(t["pl_pct"] for t in closed) / len(closed), 2) if closed else None,
        "avg_days_held": round(sum(t["days"] for t in closed) / len(closed), 1) if closed else None,
        "exits_by_rule": by_rule, "open_positions_at_end": open_end,
        "avg_invested_pct": round(sum(c[3] for c in curve) / len(curve), 1),
        "best_trade": max(closed, key=lambda t: t["pl"]) if closed else None,
        "worst_trade": min(closed, key=lambda t: t["pl"]) if closed else None,
        "curve": curve, "trades": closed[-400:], "seconds": round(time.time() - t0, 1),
    }


def line_for(entry, peak, pct=None):
    pct = TRAIL if pct is None else pct
    px = entry * (1 - STOP)
    if peak >= entry * (1 + TRAIL_ARM):
        px = max(px, peak * (1 - pct))
    return px


NAMES = ("data", "organic", "prediction", "news", "time machine")


def group_pct(bars, i, sts, entry, peak):
    """Risk group (safe / low / high) at the close of bar i -> sell-line fraction for the next day."""
    closes = [b["c"] for b in bars[max(0, i - 20):i + 1]]
    rets = [closes[k] / closes[k - 1] - 1 for k in range(1, len(closes)) if closes[k - 1]]
    vol = (sum(r * r for r in rets) / len(rets) - (sum(rets) / len(rets)) ** 2) ** 0.5 * 100 if len(rets) > 2 else None
    day = (bars[i]["c"] / bars[i - 1]["c"] - 1) * 100 if i else 0
    g, _ = AT.risk_group(dict(zip(NAMES, sts)) if sts else {}, bars[i]["c"], entry, peak, day, vol)
    return AT.LINE_PCT[g] / 100, g


def run_sim(data, dates, statuses, spy_by, t0):
    """Day by day with the live rules: morning buys (8.33% of the cash left, again and again), the sell line
    during the day, the evening rule (sell anything under +5%), bad-day and signal exits at the close."""
    cash, pos, trades, curve, picks = START_CASH, {}, [], [], []
    prev_equity, spy0 = START_CASH, None
    pick_pct, groups_seen = {}, {"safe": 0, "low": 0, "high": 0}

    def bar(s, day):
        i = data[s]["idx"].get(day)
        return data[s]["bars"][i] if i is not None else None

    def sell(s, px, day, rule, frac=1.0):
        nonlocal cash
        p = pos[s]
        px *= 1 - SLIP
        qty = p["qty"] * frac
        cash += qty * px
        trades.append({"symbol": s, "bought": p["day"], "buy_px": round(p["entry"], 4), "sold": day, "sell_px": round(px, 4),
                       "qty": round(qty, 6), "pl": round(qty * (px - p["entry"]), 2), "pl_pct": round((px / p["entry"] - 1) * 100, 2),
                       "days": (dt.date.fromisoformat(day) - dt.date.fromisoformat(p["day"])).days, "rule": rule})
        p["qty"] -= qty
        if p["qty"] <= 1e-9:
            pos.pop(s)

    for di, day in enumerate(dates):
        # 1. morning: buy yesterday's picks, each SLOT_PCT of the account at the open (or of the cash still left)
        equity_open = cash + sum(p["qty"] * (bar(s, day) or {"o": p["entry"]})["o"] for s, p in pos.items())
        cash_open, new_today = cash, 0
        for s in picks:
            if s in pos or len(pos) >= MAX_POS or (AT.SLOT_OF == "split" and new_today >= SLOTS):
                continue
            b = bar(s, day)
            amt = {"account": min(equity_open * SLOT_PCT, cash), "split": min(cash_open * SLOT_PCT, cash)}.get(AT.SLOT_OF, cash * SLOT_PCT)
            if b and amt >= 1:
                new_today += 1
            if not b or amt < 1:
                continue
            entry = b["o"] * (1 + SLIP)
            cash -= amt
            pos[s] = {"qty": amt / entry, "entry": entry, "peak": entry, "day": day, "trim": None,
                      "pct": pick_pct.get(s, AT.LINE_PCT["low"] / 100)}
        held_open = sum(p["qty"] * p["entry"] if p["day"] == day else p["qty"] * (bar(s, day) or {"o": p["entry"]})["o"] for s, p in pos.items())
        invested_day = held_open / (held_open + cash) * 100 if held_open + cash else 0
        # 2. during the day: the sell line (5% under the highest price, never under -5% from the buy)
        for s in list(pos):
            b, p = bar(s, day), pos[s]
            if not b:
                continue
            line0 = line_for(p["entry"], p["peak"], p["pct"])
            if b["l"] <= line0:
                sell(s, min(b["o"], line0), day, "trailing stop" if line0 > p["entry"] * (1 - STOP) + 1e-9 else "stop-loss")
                continue
            if TAKE > 0 and b["h"] >= p["entry"] * (1 + TAKE):
                sell(s, max(b["o"], p["entry"] * (1 + TAKE)), day, "take-profit")
                continue
            hi_line = line_for(p["entry"], max(p["peak"], b["h"]), p["pct"])
            if hi_line > line0 and b["c"] < hi_line:     # rose to a new high, then fell through the raised line
                sell(s, hi_line, day, "trailing stop")
                continue
            p["peak"] = max(p["peak"], b["h"])
        # 3. close: evening rule, bad day, signal exits
        for s in list(pos):
            b, p = bar(s, day), pos[s]
            if not b:
                continue
            if FLATTEN and b["c"] < p["entry"] * (1 + KEEP):
                sell(s, b["c"], day, f"evening under {KEEP * 100:g}%")
                continue
            st, i = statuses(s, day), data[s]["idx"].get(day)
            if not st or not i:
                continue
            sts = st[0]
            p["pct"], p["group"] = group_pct(data[s]["bars"], i, sts, p["entry"], p["peak"])
            groups_seen[p["group"]] += 1
            if b["c"] <= data[s]["bars"][i - 1]["c"] * (1 - BAD_DAY) and sts.count("ok") < 5:
                sell(s, b["c"], day, "bad day")
                continue
            sig = A.signal(sts, True)
            if sig == "sell":
                sell(s, b["c"], day, "time machine red" if sts[4] == "alert" else "2 or fewer green")
            elif sig == "start_selling" and (p["trim"] is None or di - p["trim"] >= 3):
                sell(s, b["c"], day, "3 green: sell half", 0.5)
                if s in pos:
                    pos[s]["trim"] = di
        equity = cash + sum(p["qty"] * (bar(s, day) or {"c": p["entry"]})["c"] for s, p in pos.items())
        # 4. tomorrow's buys
        picks = []
        if equity > prev_equity * (1 - DAILY_LOSS) and len(pos) < MAX_POS:
            cands = []
            for s in data:
                if s in pos:
                    continue
                st = statuses(s, day)
                if st and A.signal(st[0], False) == "buy" and st[0].count("ok") >= BUY_MIN_GREENS:
                    cands.append((st[1] or 0, st[0].count("ok"), s))
            cands.sort(reverse=True)
            picks = [s for _, _, s in cands[:MAX_POS - len(pos)]]
            pick_pct = {}
            for s in picks:
                i = data[s]["idx"].get(day)
                st = statuses(s, day)
                c = data[s]["bars"][i]["c"]
                pick_pct[s] = group_pct(data[s]["bars"], i, st[0] if st else None, c, c)[0]
        prev_equity = equity
        sb = spy_by.get(day)
        if sb:
            spy0 = spy0 or sb["c"]
        curve.append([day, round(equity, 2), round(START_CASH * sb["c"] / spy0, 2) if sb and spy0 else None,
                      round(invested_day, 1)])
    res = summarize(curve, trades, len(pos), dates, len(data), t0)
    res["group_days"] = groups_seen
    return res


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
           "rules": current_rules(),
           "caveats": ["Tested on today's 500 most-traded stocks, so stocks that faded away are missing (results look a bit better than reality).",
                       "Past headlines aren't replayed: news is assumed quiet. Data is assumed real.",
                       "Trades are simulated at daily open/high/low/close prices; real runs happen every ~5 minutes."]
                      + ["The order of each day's high and low isn't known from daily prices, so sell-line exits are approximate."]}
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
