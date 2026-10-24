#!/usr/bin/env python3
"""Keeps the trade records in the `trade-records/` folder of the repo, like a black box.

Runs on every workflow run and catches up on anything that is due, so a late or skipped
GitHub schedule never loses a report:

  excel/      the current week's Excel journal, rewritten whenever its orders change.
              A new file starts each Monday (New York date). Only the newest 3 are kept;
              older weekly Excel files are deleted (their PDFs stay forever).
  reports/YYYY/MM-Month/
              week-WW_<monday>_to_<sunday>.pdf      made from Saturday 10:00 UTC
              monthly-report_YYYY-MM_Month.pdf      made on the month's last Saturday:
                                                    title page, short monthly summary,
                                                    then that final week in full
  journal/journal.json   every order the auto-trader ever placed (the source of all reports)
  index.json             file list and storage size (warns before 1 GB)

Usage: python3 records.py RECORDS_DIR TRADES_JSON SITE_DATA_DIR [--now ISO]
"""
import datetime as dt
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import journal_report as jr  # noqa: E402

NY, UTC = jr.NY, jr.UTC
KEEP_EXCEL = 3
LIMIT_BYTES = 1024 ** 3
WARN_BYTES = int(0.9 * LIMIT_BYTES)
REPORT_HOUR_UTC = 10


def ny_date(e):
    t = jr.when(e)
    return t.astimezone(NY).date() if t else None


def monday_of(d):
    return d - dt.timedelta(days=d.weekday())


def last_saturday_of_month(sat):
    return (sat + dt.timedelta(days=7)).month != sat.month


def month_dir(d):
    return os.path.join("reports", f"{d.year}", f"{d.month:02d}-{d.strftime('%B')}")


def week_name(mon):
    return f"week-{mon.isocalendar()[1]:02d}_{mon.isoformat()}_to_{(mon + dt.timedelta(days=6)).isoformat()}"


def du(path, skip_git=True):
    total = 0
    for root, dirs, files in os.walk(path):
        if skip_git and ".git" in dirs:
            dirs.remove(".git")
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files if os.path.isfile(os.path.join(root, f)))
    return total


def main():
    rec, trades_path, site = sys.argv[1:4]
    now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]) if "--now" in sys.argv else dt.datetime.now(UTC)
    os.makedirs(os.path.join(rec, "state"), exist_ok=True)
    os.makedirs(os.path.join(rec, "excel"), exist_ok=True)
    try:
        entries = json.load(open(os.path.join(rec, "journal", "journal.json"))).get("entries", [])
    except Exception:
        entries = []
    try:
        trades = json.load(open(trades_path))
    except Exception:
        trades = {}
    state_p = os.path.join(rec, "state", "records.json")
    try:
        state = json.load(open(state_p))
    except Exception:
        state = {}
    today = now.astimezone(NY).date()
    state.setdefault("started", monday_of(today).isoformat())
    hashes = state.setdefault("excel_hashes", {})
    all_trips = jr.round_trips(entries)
    made = []

    def week_slice(mon):
        sun = mon + dt.timedelta(days=6)
        es = [e for e in entries if ny_date(e) and mon <= ny_date(e) <= sun]
        ts = [t for t in all_trips if (t["bought"] and mon <= t["bought"].astimezone(NY).date() <= sun)
              or (t["sold"] and mon <= t["sold"].astimezone(NY).date() <= sun)]
        return es, ts

    def period_label(a, b):
        return f"{a.strftime('%a %d %b %Y')} to {b.strftime('%a %d %b %Y')} (New York dates)"

    # 1. current week's Excel (rewritten only when its orders change)
    mon = monday_of(today)
    es, ts = week_slice(mon)
    xname = f"excel/{mon.isocalendar()[0]}-W{mon.isocalendar()[1]:02d}_{mon.isoformat()}.xlsx"
    digest = hashlib.sha256(json.dumps([es, trades.get("mode"), trades.get("paper")], sort_keys=True, default=str).encode()).hexdigest()
    if hashes.get(xname) != digest or not os.path.exists(os.path.join(rec, xname)):
        jr.build_xlsx(es, trades, os.path.join(rec, xname), now,
                      title=f"Trade Journal · Week {mon.isocalendar()[1]} · {mon.year}",
                      period=period_label(mon, mon + dt.timedelta(days=6)), trips=ts)
        hashes[xname] = digest
        made.append(xname)

    # 2. weekly PDFs and 3. monthly reports, catching up on every Saturday since records started
    w = dt.date.fromisoformat(state["started"])
    while w <= mon:
        sat = w + dt.timedelta(days=5)
        due = dt.datetime.combine(sat, dt.time(REPORT_HOUR_UTC), tzinfo=UTC)
        if now >= due:
            ddir = month_dir(sat)
            os.makedirs(os.path.join(rec, ddir), exist_ok=True)
            wp = os.path.join(ddir, week_name(w) + ".pdf")
            wes, wts = week_slice(w)
            wtitle = f"Weekly Trade Report · Week {w.isocalendar()[1]}, {w.year}"
            wperiod = period_label(w, w + dt.timedelta(days=6))
            if not os.path.exists(os.path.join(rec, wp)):
                jr.build_weekly_pdf(os.path.join(rec, wp), wes, wts, wtitle, wperiod, trades, now)
                made.append(wp)
            if last_saturday_of_month(sat):
                mp = os.path.join(ddir, f"monthly-report_{sat.year}-{sat.month:02d}_{sat.strftime('%B')}.pdf")
                if not os.path.exists(os.path.join(rec, mp)):
                    prev_end = sat - dt.timedelta(days=7)
                    while not last_saturday_of_month(prev_end):
                        prev_end -= dt.timedelta(days=7)
                    start = max(prev_end + dt.timedelta(days=1), dt.date.fromisoformat(state["started"]))
                    mes = [e for e in entries if ny_date(e) and start <= ny_date(e) <= sat]
                    mts = [t for t in all_trips if (t["bought"] and start <= t["bought"].astimezone(NY).date() <= sat)
                           or (t["sold"] and start <= t["sold"].astimezone(NY).date() <= sat)]
                    weeks, m = [], monday_of(start)
                    while m <= w:
                        a, b = week_slice(m)
                        weeks.append((f"Week {m.isocalendar()[1]} · {m.strftime('%d %b')} – {(m + dt.timedelta(days=6)).strftime('%d %b')}", a, b))
                        m += dt.timedelta(days=7)
                    jr.build_monthly_pdf(os.path.join(rec, mp), sat.strftime("%B %Y"), period_label(start, sat), mes, mts, weeks,
                                         (wtitle, wperiod, wes, wts), trades, now)
                    made.append(mp)
        w += dt.timedelta(days=7)

    # 4. keep only the newest weekly Excel files
    xs = sorted(f for f in os.listdir(os.path.join(rec, "excel")) if f.endswith(".xlsx"))
    removed = []
    for f in xs[:-KEEP_EXCEL]:
        os.remove(os.path.join(rec, "excel", f))
        hashes.pop(f"excel/{f}", None)
        removed.append(f"excel/{f}")

    json.dump(state, open(state_p, "w"), indent=1)

    # 5. index + size
    files = []
    for root, dirs, fs in os.walk(rec):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in fs:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, rec)
            kind = ("weekly_excel" if rel.startswith("excel/") and f.endswith(".xlsx") else "monthly_pdf" if "monthly-report" in f
                    else "weekly_pdf" if rel.startswith("reports/") and f.endswith(".pdf") else None)
            if kind:
                files.append({"path": rel, "kind": kind, "bytes": os.path.getsize(full), "name": f})
    files.sort(key=lambda x: x["path"], reverse=True)
    size = du(rec)
    git_size = du(os.path.join(rec, ".git"), skip_git=False) if os.path.isdir(os.path.join(rec, ".git")) else 0
    index = {"generated_at": now.isoformat(timespec="seconds"), "branch": "main", "folder": "trade-records", "files": files,
             "size_bytes": size, "git_bytes": git_size, "limit_bytes": LIMIT_BYTES,
             "warning": (f"Records use {(size + git_size) / 1024 ** 2:.0f} MB, close to 1 GB. Time to move them somewhere bigger."
                         if size + git_size >= WARN_BYTES else None),
             "made_this_run": made, "removed_this_run": removed, "journal_orders": len(entries)}
    json.dump(index, open(os.path.join(rec, "index.json"), "w"), indent=1)
    os.makedirs(site, exist_ok=True)
    json.dump(index, open(os.path.join(site, "records.json"), "w"))
    print(f"records: made={made} removed={removed} size={size}B git={git_size}B orders={len(entries)}")


if __name__ == "__main__":
    main()
