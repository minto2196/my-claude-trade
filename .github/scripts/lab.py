#!/usr/bin/env python3
"""Time machine lab: replays several versions of the trading rules on the same real prices and
picks one fairly.

Each version is replayed over the last ~6 months. The choice is made on the FIRST 4 months only
(training); the last 2 months (test) are never used to choose, only to check. A version passes
only if it made money in both parts and its worst drop stayed under 15%. Among the passing ones,
the best training result wins. Nothing here trades.

Usage: python3 lab.py OUT_DIR AGENT_URL_OR_PATH
"""
import datetime as dt
import itertools
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402

A, AT = B.A, B.AT
TRAIN_FRACTION = 4 / 6
MAX_DD = 15.0


def load(src):
    if src.startswith("http"):
        with urllib.request.urlopen(src + f"?t={int(time.time())}", timeout=30) as r:
            return json.loads(r.read().decode())
    return json.load(open(src))


def variants():
    out = []
    for evening, lines, strict, maxpos in itertools.product(
            (True, False), ("5/4/3", "8/7/6"), (4, 5), (40, 12)):
        out.append({"evening_rule": evening, "lines": lines, "buy_min_greens": strict, "max_positions": maxpos,
                    "name": f"{'evening <10%' if evening else 'hold overnight'} · lines {lines} · buy {strict}+/5 · max {maxpos}"})
    return out


def apply(v):
    B.FLATTEN = v["evening_rule"]
    s, l, h = (float(x) for x in v["lines"].split("/"))
    AT.LINE_PCT.update(safe=s, low=l, high=h)
    B.STOP = s / 100
    B.BUY_MIN_GREENS = v["buy_min_greens"]
    B.MAX_POS = v["max_positions"]
    B.SLOT_PCT = 1 / 12 if v["max_positions"] == 12 else 0.0833


def split(curve):
    k = int(len(curve) * TRAIN_FRACTION)
    start, mid, end = B.START_CASH, curve[k - 1][1], curve[-1][1]
    spy_mid, spy_end = curve[k - 1][2], curve[-1][2]
    return {"train_from": curve[0][0], "train_to": curve[k - 1][0], "test_from": curve[k][0], "test_to": curve[-1][0],
            "train_pct": round((mid / start - 1) * 100, 2), "test_pct": round((end / mid - 1) * 100, 2),
            "spy_train_pct": round((spy_mid / B.START_CASH - 1) * 100, 2) if spy_mid else None,
            "spy_test_pct": round((spy_end / spy_mid - 1) * 100, 2) if spy_mid and spy_end else None}


def main():
    out_dir, agent_src = sys.argv[1], sys.argv[2]
    t0 = time.time()
    agent = load(agent_src)
    cols = agent.get("board_columns", [])
    syms = [r[cols.index("symbol")] for r in agent.get("board", []) if not r[cols.index("symbol")].endswith("USD")]
    days = int((B.SIM_DAYS + A.TM_LOOKBACK + 110) * 1.47)
    daily = A.bars(syms, False, "1Day", days, "Lab", per=25)
    spy = A.bars(["SPY"], False, "1Day", days, "Lab").get("SPY", [])
    data, dates, statuses, spy_by, _ = B.prepare(daily, spy, B.SIM_DAYS)
    print(f"lab: {len(data)} stocks, {len(dates)} days prepared in {time.time() - t0:.0f}s")
    rows = []
    for v in variants():
        apply(v)
        r = B.run_sim(data, dates, statuses, spy_by, time.time())
        row = dict(v, **split(r["curve"]), total_pct=r["total_return_pct"], spy_pct=r["spy_return_pct"],
                   max_drawdown_pct=r["max_drawdown_pct"], trades=r["trades_closed"], win_rate=r["win_rate"],
                   avg_trade_pct=r["avg_trade_pct"], avg_days_held=r["avg_days_held"], exits=r["exits_by_rule"])
        row["passes"] = row["train_pct"] > 0 and row["test_pct"] > 0 and row["max_drawdown_pct"] < MAX_DD
        rows.append(row)
        print(f"lab: {v['name']:<52} train {row['train_pct']:+6.2f}%  test {row['test_pct']:+6.2f}%  "
              f"total {row['total_pct']:+6.2f}%  dd {row['max_drawdown_pct']:5.2f}%  trades {row['trades']:5d}  {'PASS' if row['passes'] else ''}")
    passing = [r for r in rows if r["passes"]]
    winner = max(passing, key=lambda r: r["train_pct"]) if passing else None
    now = dt.datetime.now(dt.timezone.utc)
    res = {"generated_at": now.isoformat(timespec="seconds"), "stocks": len(data), "days": len(dates),
           "rule": "chosen on the first 4 months only; must also make money in the last 2 months, worst drop under 15%",
           "winner": winner, "variants": sorted(rows, key=lambda r: -r["train_pct"]), "seconds": round(time.time() - t0)}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(res, open(os.path.join(out_dir, f"lab_{now.date().isoformat()}.json"), "w"), indent=1)
    json.dump(res, open(os.path.join(out_dir, "lab_latest.json"), "w"), indent=1)
    print("lab: winner:", winner["name"] if winner else "none passed")


if __name__ == "__main__":
    main()
