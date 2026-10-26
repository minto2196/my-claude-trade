#!/usr/bin/env python3
"""Five supervisor agents that check the whole Trade Desk every hour. Read-only.

  Data watch      Alpaca answers; the portfolio snapshot is fresh, error-free and on the
                  account type you chose (paper or live).
  Scan watch      the verification agent ran recently; the funnel is the right size; check 1
                  (data is real) passes for most stocks, otherwise the market data feed is suspect;
                  the time machine (check 5) judged the shortlist.
  Trade watch     the auto-trader's switch and the time machine master switch; rejected orders; positions past the stop-loss that
                  were not sold (a rule that failed to fire); margin use; stuck open orders;
                  more positions than slots.
  Records watch   every auto-trader order in Alpaca is in the journal; this week's Excel exists;
                  Saturday's PDF and the monthly report exist once due; storage is under 1 GB.
  Schedule watch  GitHub keeps running the workflow: recent failures, long gaps between runs
                  while the market is open.

Runs on every workflow run, but checks only once an hour; in between it republishes the last result.
Usage: python3 supervisor.py SITE_DATA_DIR RECORDS_DIR [--force]
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

KEY = os.environ.get("ALPACA_API_KEY", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"
TRADE = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
STOP = float(os.environ.get("AUTOTRADE_STOP_LOSS_PCT", "5") or 5)
SLOTS = int(os.environ.get("AUTOTRADE_MAX_POSITIONS", "40") or 40)
KEEP = float(os.environ.get("AUTOTRADE_KEEP_OVERNIGHT_PCT", "10") or 10)
EVERY_MIN = 55
UTC = dt.timezone.utc
RANK = {"ok": 0, "warn": 1, "alert": 2}


def alpaca(path, params=None):
    url = TRADE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def github(path, params=None):
    url = f"https://api.github.com/repos/{GH_REPO}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception:
        return None


def age_min(x, now):
    t = ts(x)
    return (now - t).total_seconds() / 60 if t else None


class Sup:
    def __init__(self, name, role):
        self.name, self.role, self.checks = name, role, []

    def add(self, status, text):
        self.checks.append({"status": status, "text": text})

    def ok(self, text): self.add("ok", text)
    def warn(self, text): self.add("warn", text)
    def alert(self, text): self.add("alert", text)

    def guard(self, fn):
        try:
            fn(self)
        except Exception as e:
            self.alert(f"This supervisor could not finish its check: {type(e).__name__}: {str(e)[:160]}")

    def result(self):
        st = max((c["status"] for c in self.checks), key=RANK.get, default="ok")
        return {"name": self.name, "role": self.role, "status": st, "checks": self.checks}


def main():
    site, rec = sys.argv[1], sys.argv[2]
    now = dt.datetime.now(UTC)
    state_p = os.path.join(rec, "state", "supervisor.json")
    prev = load(state_p)
    out_p = os.path.join(site, "supervisor.json")
    if prev and "--force" not in sys.argv and (age_min(prev.get("generated_at"), now) or 1e9) < EVERY_MIN:
        json.dump(prev, open(out_p, "w"))
        print(f"supervisor: not due (last {prev.get('generated_at')}), republished")
        return

    portfolio = load(os.path.join(site, "portfolio.json")) or {}
    agent = load(os.path.join(site, "agent.json")) or {}
    trades = load(os.path.join(site, "trades.json")) or {}
    backtest = load(os.path.join(site, "backtest.json")) or {}
    index = load(os.path.join(rec, "index.json")) or {}
    journal = (load(os.path.join(rec, "journal", "journal.json")) or {}).get("entries", [])
    clock = {}
    market_open = False

    def data(s):
        nonlocal clock, market_open
        if not (KEY and SECRET):
            s.alert("Alpaca keys are missing from the repository secrets.")
            return
        acct = alpaca("/v2/account")
        clock = alpaca("/v2/clock")
        market_open = bool(clock.get("is_open"))
        s.ok(f"Alpaca answers ({'paper' if PAPER else 'LIVE'} account, status {acct.get('status', '?')}).")
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            s.alert("Alpaca says trading is blocked on this account.")
        if portfolio.get("error"):
            s.alert(f"The last portfolio snapshot failed: {portfolio['error']}")
        a = age_min(portfolio.get("generated_at"), now)
        if a is None:
            s.alert("No portfolio snapshot has been published yet.")
        elif a > 20:
            s.warn(f"The portfolio snapshot is {a:.0f} minutes old.")
        else:
            s.ok(f"Portfolio snapshot is fresh ({a:.0f} min old).")
        if portfolio and bool(portfolio.get("paper")) != PAPER:
            s.alert("The dashboard shows a different account type (paper/live) than the current setting.")

    def scan(s):
        if agent.get("error"):
            s.alert(f"The verification agent's last run failed: {agent['error']}")
        a = age_min(agent.get("generated_at"), now)
        if a is None or a > 20:
            s.warn(f"The agent's results are {'missing' if a is None else f'{a:.0f} minutes old'}.")
        else:
            s.ok(f"Agent ran {a:.0f} min ago.")
        f = agent.get("funnel") or {}
        if f:
            if f.get("tradable", 0) < 5000 or f.get("observed", 0) < 4000 or f.get("shortlisted", 0) < 400:
                s.warn(f"The funnel looks small: {f.get('tradable')} tradable, {f.get('observed')} observed, {f.get('shortlisted')} shortlisted. "
                       "The market data may have come back incomplete.")
            else:
                s.ok(f"Funnel normal: {f['tradable']:,} → {f['observed']:,} → {f['shortlisted']} → {f.get('buy', 0)} buy-ready (4+/5 green).")
            checked = f.get("checked") or 1
            p1 = (f.get("passed") or {}).get("price", 0) / checked
            if p1 < 0.8:
                s.alert(f"Only {p1:.0%} of stocks pass 'data is real'. The price feed itself may be broken; buys should not be trusted.")
            else:
                s.ok(f"{p1:.0%} of stocks pass 'data is real'.")
            if f.get("buy", 0) > 0.3 * checked:
                s.warn(f"{f['buy']} of {checked} stocks are buy-ready. That is unusually many; the checks may be too loose today.")
            tm = (f.get("passed") or {}).get("history")
            if tm is None:
                s.alert("The time machine (check 5) didn't judge the shortlist. Nothing can pass the veto, so nothing will be bought.")
            else:
                s.ok(f"Time machine judged the shortlist: {tm} of {checked} have a green past.")

    def trade(s):
        mode = trades.get("mode", "off")
        s.ok(f"Auto-trader switch is {mode.upper()} on a {'paper' if PAPER else 'LIVE'} account.")
        if not backtest:
            s.warn("No time machine result yet, so the master switch keeps buying paused.")
        else:
            bage = age_min(backtest.get("generated_at"), now)
            if backtest.get("error"):
                s.warn(f"The last time machine replay failed: {backtest['error'][:140]}")
            if bage is not None and bage > 10 * 24 * 60:
                s.alert(f"The time machine result is {bage / 1440:.0f} days old; buying is paused until a new replay runs.")
            elif backtest.get("buys_allowed"):
                s.ok(f"Master switch ON: the 6-month replay returned {backtest.get('total_return_pct')}% "
                     f"(S&P 500 {backtest.get('spy_return_pct')}%), max drop {backtest.get('max_drawdown_pct')}%.")
            else:
                s.warn(f"Master switch OFF: the 6-month replay returned {backtest.get('total_return_pct')}%. Buying is paused, exits still run.")
        if trades.get("error"):
            s.alert(f"The auto-trader's last run failed: {trades['error']}")
        day_ago = now - dt.timedelta(days=1)
        rej = [e for e in journal if e.get("status") == "rejected" and (ts(e.get("decided_at_utc")) or now) > day_ago]
        if rej:
            s.warn(f"{len(rej)} order(s) rejected in the last 24 h, e.g. {rej[-1].get('symbol')}: {(rej[-1].get('error') or '')[:120]}")
        if not (KEY and SECRET):
            return
        positions = alpaca("/v2/positions")
        orders = alpaca("/v2/orders", {"status": "open", "limit": 200})
        owned = set(trades.get("owned") or [])
        late = [p["symbol"] for p in positions if p["symbol"] in owned and float(p.get("unrealized_plpc") or 0) * 100 <= -(STOP + 2)]
        if late:
            s.alert(f"Past the {STOP:g}% stop-loss but still held: {', '.join(late)}. Check the auto-trader runs.")
        if len(owned) > SLOTS:
            s.alert(f"The auto-trader holds {len(owned)} positions, more than its maximum of {SLOTS}.")
        acct = alpaca("/v2/account")
        if float(acct.get("cash") or 0) < 0:
            s.alert("Cash is negative: the account is using margin.")
        stops = {o["symbol"] for o in orders if (o.get("client_order_id") or "").startswith("tdagent-stop-")}
        held_syms = {p["symbol"] for p in positions if p.get("asset_class") != "crypto"}
        unprotected = sorted((owned & held_syms) - stops)
        if market_open and unprotected:
            s.warn(f"No Alpaca stop order (−{STOP:g}%) on: {', '.join(unprotected)}. The next auto-trader run should place it.")
        elif owned & held_syms:
            s.ok(f"Alpaca stop orders at −{STOP:g}% protect {len((owned & held_syms) & stops)} position(s)." if market_open
                 else f"Market closed; Alpaca stop orders are renewed at the first run after the open.")
        if os.environ.get("AUTOTRADE_FLATTEN", "true").lower() != "false" and owned & held_syms:
            ny_today = now.astimezone(dt.timezone(dt.timedelta(hours=-5))).date().isoformat()
            last_buy = {}
            for e in journal:
                if e.get("side") == "buy" and e.get("filled_at_utc"):
                    last_buy[e["symbol"]] = max(last_buy.get(e["symbol"], ""), e["filled_at_utc"][:10])
            pmap = {p["symbol"]: p for p in positions}
            def closed_pl(x):
                p = pmap[x]
                e, c = float(p.get("avg_entry_price") or 0), float(p.get("lastday_price") or 0)
                return (c / e - 1) * 100 if e and c else None
            overnight = sorted(x for x in owned & held_syms if last_buy.get(x, ny_today) < ny_today
                               and closed_pl(x) is not None and closed_pl(x) < KEEP)
            kept = sorted(x for x in owned & held_syms if last_buy.get(x, ny_today) < ny_today and x not in overnight)
            if overnight:
                s.alert(f"Held overnight under +{KEEP:g}% although the evening rule sells those: {', '.join(overnight)}. "
                        "The first run after the open sells them; check why the evening run was missed.")
            if kept:
                s.ok(f"Kept overnight at +{KEEP:g}% or more, as the rules allow: {', '.join(kept)}.")
        stuck = [o["symbol"] for o in orders if (age_min(o.get("submitted_at"), now) or 0) > 24 * 60]
        if stuck:
            s.warn(f"Orders open for more than a day: {', '.join(stuck)}.")
        if not late and not stuck:
            s.ok(f"{len(positions)} position(s), {len(orders)} open order(s), no stop-loss missed.")

    def records(s):
        if KEY and SECRET:
            since = (now - dt.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            mine = [o for o in alpaca("/v2/orders", {"status": "all", "limit": 500, "after": since})
                    if (o.get("client_order_id") or "").startswith("tdagent-")
                    and not ((o.get("client_order_id") or "").startswith("tdagent-stop-") and o.get("status") != "filled")]
            known = {e.get("order_id") for e in journal}
            missing = [o["symbol"] for o in mine if o["id"] not in known]
            if missing:
                s.warn(f"{len(missing)} auto-trader order(s) in Alpaca are not in the journal yet ({', '.join(missing[:5])}). They will be recovered next run.")
            else:
                s.ok(f"Journal complete: {len(journal)} order(s) recorded.")
        files = index.get("files") or []
        xl = [f for f in files if f["kind"] == "weekly_excel"]
        ny = now.astimezone(dt.timezone(dt.timedelta(hours=-4))).date()
        mon = ny - dt.timedelta(days=ny.weekday())
        if not any(mon.isoformat() in f["name"] for f in xl):
            s.warn("This week's Excel journal hasn't been created yet.")
        else:
            s.ok("This week's Excel journal exists.")
        if len(xl) > 3:
            s.warn(f"{len(xl)} weekly Excel files kept; only 3 should be.")
        sat = mon + dt.timedelta(days=5)
        if now >= dt.datetime.combine(sat, dt.time(10, 30), tzinfo=UTC):
            if not any(f["kind"] == "weekly_pdf" and f"_{mon.isoformat()}_" in f["name"] for f in files):
                s.alert("Saturday's weekly PDF is overdue.")
            else:
                s.ok("This week's PDF report is filed.")
        size = (index.get("size_bytes") or 0) + (index.get("git_bytes") or 0)
        if GH_TOKEN and GH_REPO:
            try:   # the whole repository on GitHub, history included (GitHub reports KB)
                size = max(size, int(github("").get("size", 0)) * 1024)
            except Exception:
                pass
        if size >= 0.9 * 1024 ** 3:
            s.alert(f"The repository uses {size / 1024 ** 2:.0f} MB, close to the 1 GB limit. Time to move the records.")
        else:
            s.ok(f"Repository storage {size / 1024 ** 2:.1f} MB of 1 GB.")

    def schedule(s):
        if not (GH_TOKEN and GH_REPO):
            s.warn("No GitHub token, so workflow health can't be checked.")
            return
        runs = github("/actions/workflows/dashboard.yml/runs", {"per_page": 40}).get("workflow_runs", [])
        recent = [r for r in runs if (age_min(r.get("created_at"), now) or 1e9) < 180]
        failed = [r for r in recent if r.get("conclusion") == "failure"]
        if failed:
            s.alert(f"{len(failed)} failed run(s) in the last 3 hours. Latest: {failed[0].get('html_url')}")
        times = sorted(ts(r["created_at"]) for r in recent if r.get("conclusion") == "success")
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
        if times:
            gaps.append((now - times[-1]).total_seconds() / 60)
        worst = max(gaps, default=None)
        if market_open and worst and worst > 30:
            s.warn(f"GitHub left a {worst:.0f}-minute gap between runs while the market was open. Exits can only act when a run happens.")
        if not failed:
            s.ok(f"{len(recent)} run(s) in the last 3 hours, none failed" + (f", longest gap {worst:.0f} min." if worst else "."))
        wd = load(os.path.join(rec, "state", "watchdog.json")) or {}
        wage = age_min(wd.get("generated_at"), now)
        if market_open:
            if wage is None or wage > 15:
                s.alert("The every-minute watchdog hasn't reported for "
                        + ("ever" if wage is None else f"{wage:.0f} minutes") + " while the market is open. Held stocks are only checked every 5 minutes.")
            else:
                s.ok(f"Every-minute watchdog alive: {wd.get('checks', 0)} checks today, last saved {wage:.0f} min ago.")

    sups = [Sup("Data watch", "Alpaca connection and portfolio snapshot"),
            Sup("Scan watch", "Market scan and the four checks"),
            Sup("Trade watch", "Auto-trader rules, positions and orders"),
            Sup("Records watch", "Journal, Excel and PDF reports, storage"),
            Sup("Schedule watch", "GitHub Actions runs and gaps")]
    for sup, fn in zip(sups, (data, scan, trade, records, schedule)):
        sup.guard(fn)
    res = [s.result() for s in sups]
    comments = [{"level": c["status"], "supervisor": r["name"], "text": c["text"]}
                for r in res for c in r["checks"] if c["status"] != "ok"]
    out = {"generated_at": now.isoformat(timespec="seconds"),
           "next_check": (now + dt.timedelta(minutes=60)).isoformat(timespec="seconds"),
           "overall": max((r["status"] for r in res), key=RANK.get), "supervisors": res, "comments": comments}
    os.makedirs(os.path.dirname(state_p), exist_ok=True)
    json.dump(out, open(state_p, "w"), indent=1)
    json.dump(out, open(out_p, "w"))
    print(f"supervisor: overall={out['overall']} comments={len(comments)}")


if __name__ == "__main__":
    main()
