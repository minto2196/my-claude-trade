#!/usr/bin/env python3
"""Every-minute watchdog for the stocks the auto-trader holds.

Runs as its own GitHub job during US market hours and checks every held stock every 60 seconds:
  * sorts it into Safe / Low risk / High risk (same rules as the auto-trader)
  * sell line: 5% (safe), 4% (low risk), 3% (high risk) below the highest price since buying,
    never lower than 5% under the buy price; at or under the line -> sells at once
  * high risk and the checks say "sell" (2 or fewer green, or a red time machine) -> sells
  * raises the Alpaca stop order to the line as prices climb (never lowers it)
  * evening rule: in the last 30 minutes, sells everything under +10% from its buy price
  * respects the AUTOTRADE kill switch: off = watches only, sells nothing

The 5-minute runs keep doing the market scan and the buying. Every sale here uses a client order id
starting with tdagent-wd-, so the journal records it with its reason.

Usage: python3 watchdog.py RECORDS_DIR AGENT_URL [--minutes 350]
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotrader as T  # noqa: E402

UTC = dt.timezone.utc
num = T.num


def fetch_json(url):
    try:
        with urllib.request.urlopen(url + ("&" if "?" in url else "?") + f"t={int(time.time())}", timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def git_save(rec, msg):
    """Commit the watchdog status so the dashboard and supervisors can see it."""
    try:
        subprocess.run(["git", "add", os.path.join(rec, "state", "watchdog.json")], check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", msg], check=True, capture_output=True)
        for _ in range(3):
            if subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], capture_output=True).returncode == 0 and \
               subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], capture_output=True).returncode == 0:
                return
            time.sleep(5)
    except Exception as e:
        print("save failed:", e)


def main():
    rec, agent_url = sys.argv[1], sys.argv[2]
    budget_min = float(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 350
    started = dt.datetime.now(UTC)
    status_p = os.path.join(rec, "state", "watchdog.json")
    try:
        peaks = json.load(open(os.path.join(rec, "state", "peaks.json"))).get("peaks", {})
    except Exception:
        peaks = {}
    try:
        prev = json.load(open(status_p))
    except Exception:
        prev = {}
    today = T.et_today()
    actions = prev.get("actions_today", []) if prev.get("day") == today else []
    status = {"started_at": started.isoformat(timespec="seconds"), "day": today, "mode": T.MODE, "paper": T.PAPER,
              "line_pct": T.LINE_PCT, "checks": 0, "actions_today": actions}
    if not (T.KEY and T.SECRET):
        print("watchdog: no Alpaca keys")
        return
    clock = T.call("GET", "/v2/clock")
    if not clock.get("is_open"):
        wait = (T.iso_utc(clock["next_open"]) - dt.datetime.now(UTC)).total_seconds()
        if 0 < wait <= 20 * 60:
            print(f"watchdog: market opens in {wait / 60:.1f} min, waiting")
            time.sleep(wait + 20)
            clock = T.call("GET", "/v2/clock")
    if not clock.get("is_open"):
        print(f"watchdog: market closed (next open {clock.get('next_open')}), nothing to watch")
        return
    close_at = T.iso_utc(clock["next_close"])
    agent, agent_t, owned, owned_t, last_save, bought_today = None, 0, set(), 0, time.time(), set()
    account = T.call("GET", "/v2/account")
    print(f"watchdog: started, market closes {close_at.isoformat()}, mode={T.MODE}, paper={T.PAPER}")

    while (dt.datetime.now(UTC) - started).total_seconds() / 60 < budget_min:
        loop_t = time.time()
        now = dt.datetime.now(UTC)
        minutes_left = (close_at - now).total_seconds() / 60
        if minutes_left <= 0:
            break
        try:
            if time.time() - agent_t > 240:
                agent = fetch_json(agent_url) or agent
                agent_t = time.time()
            if time.time() - owned_t > 600:
                orders = T.fetch_orders(60)
                mine = [o for o in orders if (o.get("client_order_id") or "").startswith(T.TAG)]
                owned = {o["symbol"] for o in mine if o.get("side") == "buy" and o.get("status") in ("filled", "partially_filled")}
                bought_today = {o["symbol"] for o in mine if o.get("side") == "buy" and (o.get("filled_at") or "")[:10] == today}
                account = T.call("GET", "/v2/account")
                owned_t = time.time()
            positions = {p["symbol"]: p for p in T.call("GET", "/v2/positions")}
            open_orders = T.call("GET", "/v2/orders", {"status": "open", "limit": 500})
            stops, busy = {}, set()
            for o in open_orders:
                if (o.get("client_order_id") or "").startswith(T.STOP_TAG):
                    stops.setdefault(o["symbol"], []).append(o)
                else:
                    busy.add(o["symbol"])
            agent = agent or {}
            cols = agent.get("board_columns", [])
            board = {r[cols.index("symbol")]: dict(zip(cols, r)) for r in agent.get("board", [])} if cols else {}
            detail = {d["symbol"]: d for d in agent.get("symbols", [])}
            pdt_limited = (not T.PAPER and num(account.get("equity")) < 25000 and int(num(account.get("daytrade_count"))) >= 3)
            groups = []

            def sell(sym, qty, code, reason):
                for o in stops.pop(sym, []):
                    try:
                        T.call("DELETE", f"/v2/orders/{o['id']}")
                    except Exception:
                        pass
                time.sleep(1)
                body = {"symbol": sym, "side": "sell", "type": "market", "time_in_force": "day",
                        "qty": f"{qty:.9f}".rstrip("0").rstrip("."), "client_order_id": f"{T.TAG}wd-{code}-{sym}-{int(time.time()*1000)}"[:48]}
                act = {"time": dt.datetime.now(UTC).isoformat(timespec="seconds"), "symbol": sym, "side": "sell", "qty": body["qty"], "reason": reason}
                try:
                    o = T.call("POST", "/v2/orders", body=body)
                    act["status"] = o.get("status")
                except Exception as e:
                    act.update(status="rejected", error=str(e)[:160])
                actions.append(act)
                print("watchdog:", act)

            for sym in sorted(owned & set(positions)):
                p = positions[sym]
                if p.get("asset_class") == "crypto":
                    continue
                qty, entry, price = num(p.get("qty")), num(p.get("avg_entry_price")), num(p.get("current_price"))
                if qty <= 0 or not entry or not price:
                    continue
                peak = max(num(peaks.get(sym)), price, entry)
                peaks[sym] = peak
                day = num(p.get("change_today")) * 100
                plpc = (price / entry - 1) * 100
                st, vol = T.group_inputs(sym, board, detail)
                group, why = T.risk_group(st, price, entry, peak, day, vol)
                line = T.stop_line(entry, peak, T.LINE_PCT[group])
                groups.append({"symbol": sym, "group": group, "label": T.GROUP_LABEL[group], "reasons": why, "price": price,
                               "entry": round(entry, 4), "highest": round(peak, 4), "line": line, "line_pct": T.LINE_PCT[group],
                               "plpc": round(plpc, 2), "day_pct": round(day, 2), "to_line_pct": round((price / line - 1) * 100, 2)})
                if T.MODE == "off" or sym in busy:
                    continue
                if pdt_limited and sym in bought_today:
                    continue
                if T.FLATTEN and minutes_left <= T.FLATTEN_MINUTES and plpc < T.KEEP_OVERNIGHT_PCT:
                    sell(sym, qty, "evening", f"Evening: {plpc:+.1f}% (under +{T.KEEP_OVERNIGHT_PCT:g}%), {round(minutes_left)} min before the close")
                elif price <= line:
                    sell(sym, qty, "line", f"{T.GROUP_LABEL[group]}: price ${price:,.2f} reached its sell line ${line:,.2f} "
                                           f"({T.LINE_PCT[group]:g}% under the high ${peak:,.2f}), {plpc:+.1f}%")
                elif group == "high" and (board.get(sym) or {}).get("signal") == "sell":
                    sell(sym, qty, "signal", f"High risk and the checks say sell ({'; '.join(why)}), {plpc:+.1f}%")
                elif not (T.FLATTEN and minutes_left <= T.FLATTEN_MINUTES):
                    have = stops.get(sym, [])
                    if len(have) == 1 and abs(num(have[0].get("qty")) - qty) < 1e-6 and num(have[0].get("stop_price")) >= line - 0.005:
                        continue
                    for o in have:
                        try:
                            T.call("DELETE", f"/v2/orders/{o['id']}")
                        except Exception:
                            pass
                    if have:
                        time.sleep(1)
                    try:
                        T.call("POST", "/v2/orders", body={"symbol": sym, "side": "sell", "type": "stop", "stop_price": f"{line}",
                                                          "time_in_force": "day", "qty": f"{qty:.9f}".rstrip("0").rstrip("."),
                                                          "client_order_id": f"{T.STOP_TAG}{sym}-{int(time.time()*1000)}"[:48]})
                    except Exception as e:
                        print(f"watchdog: stop order for {sym} refused: {str(e)[:120]}")
            status.update(generated_at=dt.datetime.now(UTC).isoformat(timespec="seconds"), checks=status["checks"] + 1,
                          minutes_to_close=round(minutes_left), groups=sorted(groups, key=lambda g: ({"high": 0, "low": 1, "safe": 2}[g["group"]], g["symbol"])),
                          counts={k: sum(1 for g in groups if g["group"] == k) for k in ("safe", "low", "high")},
                          actions_today=actions[-100:], error=None, peaks=peaks)
        except Exception as e:
            status.update(error=f"{type(e).__name__}: {str(e)[:200]}", generated_at=dt.datetime.now(UTC).isoformat(timespec="seconds"))
            print("watchdog error:", status["error"])
        os.makedirs(os.path.dirname(status_p), exist_ok=True)
        json.dump(status, open(status_p, "w"), indent=1)
        if time.time() - last_save > 600:
            git_save(rec, f"watchdog: {status.get('generated_at', '')[:16]} UTC, {status['checks']} checks")
            last_save = time.time()
        time.sleep(max(1, 60 - (time.time() - loop_t)))
    status["ended_at"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    json.dump(status, open(status_p, "w"), indent=1)
    git_save(rec, f"watchdog: ended after {status['checks']} checks")
    print(f"watchdog: ended after {status['checks']} checks, {len(actions)} actions today")


if __name__ == "__main__":
    main()
