#!/usr/bin/env python3
"""Every-minute watchdog for the stocks the auto-trader holds.

Runs as its own GitHub job during US market hours and checks every held stock every 60 seconds:
  * sorts it into Safe / Low risk / High risk (same rules as the auto-trader)
  * sell line: 5% below the highest price since buying (every risk group, by default),
    never lower than 5% under the buy price; at or under the line -> sells at once
  * high risk and the checks say "sell" (2 or fewer green, or a red time machine) -> sells
  * raises the Alpaca stop order to the line as prices climb (never lowers it)
  * evening rule: in the last 30 minutes, sells everything under +5% from its buy price
  * respects the AUTOTRADE kill switch: off = watches only, sells nothing

The 5-minute runs keep doing the market scan and the buying. Every sale here uses a client order id
starting with tdagent-wd-, so the journal records it with its reason.

It also keeps the whole system running even when GitHub's scheduler is late: while the market is
open it starts the 5-minute dashboard run itself (scan, auto-trader, records, site), and before its
own time limit it starts the next watchdog. `--ensure` (used by every dashboard run) starts the
watchdog if the market is open or opening within 20 minutes and no watchdog is running.

Usage: python3 watchdog.py RECORDS_DIR AGENT_URL [--minutes 350]
       python3 watchdog.py --ensure
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


GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
# Stay awake: outside market hours the watchdog sleeps and hands over to the next one, so a watchdog is
# always alive and starts the scan itself just before the open (GitHub's scheduler can be hours late).
STAY_AWAKE = os.environ.get("WATCHDOG_STAY_AWAKE", "true").strip().lower() != "false"


def gh(method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def gh_active(workflow):
    """Is a run of this workflow queued or running?"""
    try:
        for st in ("in_progress", "queued"):
            if gh("GET", f"/actions/workflows/{workflow}/runs?status={st}&per_page=5").get("total_count", 0):
                return True
    except Exception as e:
        print("watchdog: can't read workflow runs:", str(e)[:120])
        return True   # unknown: don't pile up runs
    return False


def gh_start(workflow):
    if not (GH_TOKEN and GH_REPO):
        return False
    try:
        gh("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": "main"})
        print(f"watchdog: started {workflow}")
        return True
    except Exception as e:
        print(f"watchdog: couldn't start {workflow}: {str(e)[:120]}")
        return False


def ensure():
    """Called by every dashboard run: start the watchdog if the market is (about to be) open and none runs."""
    if not (T.KEY and T.SECRET and GH_TOKEN):
        return
    clock = T.call("GET", "/v2/clock")
    soon = (T.iso_utc(clock["next_open"]) - dt.datetime.now(UTC)).total_seconds() <= 20 * 60
    awake = STAY_AWAKE and T.MODE != "off"   # stay-awake: a watchdog should always be alive, open or closed
    if (clock.get("is_open") or soon or awake) and not gh_active("watchdog.yml"):
        gh_start("watchdog.yml")
    else:
        print("watchdog ensure: nothing to do", "(open)" if clock.get("is_open") else "(closed)")


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


def sleep_until_open(rec, status, status_p, clock, started, budget_min):
    """Market closed: sleep, start a fresh scan ~6 minutes before the open, then wait for the bell.
    Returns True when the open has come; False after handing over to the next watchdog (time limit)."""
    next_open = T.iso_utc(clock["next_open"])
    deadline = started + dt.timedelta(minutes=budget_min)
    status.update(state="sleeping", next_open=next_open.isoformat(), generated_at=dt.datetime.now(UTC).isoformat(timespec="seconds"))
    json.dump(status, open(status_p, "w"), indent=1)
    git_save(rec, f"watchdog: sleeping until the open {next_open.isoformat()[:16]} UTC")
    print(f"watchdog: market closed, sleeping until {next_open.isoformat()} (wake-up scan 6 min before)")
    kicked = False
    while True:
        now = dt.datetime.now(UTC)
        to_open = (next_open - now).total_seconds()
        if to_open <= 0:
            return True
        if not kicked and to_open <= 6 * 60:
            if not gh_active("dashboard.yml"):
                gh_start("dashboard.yml")      # fresh scan and dashboard right before the bell
            kicked = True
        if (deadline - now).total_seconds() < 5 * 60 and to_open > 5 * 60:
            gh_start("watchdog.yml")           # time limit: the next watchdog keeps sleeping
            status.update(state="handed over", ended_at=now.isoformat(timespec="seconds"))
            json.dump(status, open(status_p, "w"), indent=1)
            print("watchdog: time limit while the market is closed, handed over to the next watchdog")
            return False
        time.sleep(max(1, min(60, to_open)))


def main():
    if "--ensure" in sys.argv:
        ensure()
        return
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
    if not clock.get("is_open") and STAY_AWAKE and T.MODE != "off":
        if not sleep_until_open(rec, status, status_p, clock, started, budget_min):
            return
        for _ in range(10):                    # Alpaca's clock flips a moment after the bell
            clock = T.call("GET", "/v2/clock")
            if clock.get("is_open"):
                break
            time.sleep(30)
        if not clock.get("is_open"):           # a holiday or a delayed open: start again and sleep on
            gh_start("watchdog.yml")
            return
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
    last_kick = 0
    account = T.call("GET", "/v2/account")
    status["state"] = "watching"
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
                if T.MODE in ("off", "sell-all") or sym in busy:   # sell-all: the 5-minute run sells everything
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
        if time.time() - last_kick > 300:      # the 5-minute scan/trade/site run, independent of GitHub's scheduler
            if not gh_active("dashboard.yml"):
                gh_start("dashboard.yml")
            last_kick = time.time()
        os.makedirs(os.path.dirname(status_p), exist_ok=True)
        json.dump(status, open(status_p, "w"), indent=1)
        if time.time() - last_save > 600:
            git_save(rec, f"watchdog: {status.get('generated_at', '')[:16]} UTC, {status['checks']} checks")
            last_save = time.time()
        time.sleep(max(1, 60 - (time.time() - loop_t)))
    if (close_at - dt.datetime.now(UTC)).total_seconds() > 120:
        gh_start("watchdog.yml")             # time limit reached while the market is still open: hand over
    elif STAY_AWAKE and T.MODE != "off":
        gh_start("watchdog.yml")             # after the close: the next watchdog sleeps until the next open
    status["ended_at"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    json.dump(status, open(status_p, "w"), indent=1)
    git_save(rec, f"watchdog: ended after {status['checks']} checks")
    print(f"watchdog: ended after {status['checks']} checks, {len(actions)} actions today")


if __name__ == "__main__":
    main()
