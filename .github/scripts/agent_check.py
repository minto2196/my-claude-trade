#!/usr/bin/env python3
"""Market-wide verification agent for the Trade Desk dashboard.

A funnel, run by teams of parallel workers inside the GitHub Action:

  Scouts      read every active, tradable US stock on Alpaca and take a market
              snapshot of each one; the most-traded OBSERVE (5000) are "observed".
  Screeners   shortlist SHORTLIST (500) of those: price at least MIN_PRICE and
              ranked by dollar volume. Your holdings and watchlist always join.
  Verifiers   run four checks on every shortlisted stock:
                1. Data is real   price agrees across trade, quote, minute bar,
                                  daily bar and your position; the daily bars are
                                  sane (no holes, no broken highs/lows); the daily,
                                  20-day and hourly trends agree.
                2. Organic trend  no volume pump without news, no one-day jump doing
                                  all the work, no gaps, no pump-and-fade, not thin.
                3. Prediction     a 5-trading-day call. Green only when it leans up.
                                  Every call is logged and scored when its date passes.
                4. News impact    headlines in the last 72 hours (only for stocks that
                                  can still reach a buy, plus holdings).
                5. Time machine   replays this stock's past year: on the days the checks
                                  would have been green, did it go up over the next 5
                                  trading days? Uses only what was known on each day.
  Advisor     turns green flags into signals, never orders. Hierarchy:
                vetoes: time machine (5) and data is real (1) must both be green
                then at least 2 of organic (2), prediction (3), news (4)
                not held, 4 or 5 of 5 green, vetoes green, no red -> buy candidate
                held,     4 or 5 of 5 green                        -> hold
                held,     3 of 5 green                             -> start selling
                held,     2 or fewer, or time machine red          -> sell

Read-only: nothing here places, changes or cancels an order. Standard library only.

Usage:
  python3 agent_check.py OUT.json --portfolio portfolio.json [--previous URL_OR_PATH] [--watchlist SYM,SYM]
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import statistics as st
import time
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("ALPACA_API_KEY", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"
TRADE = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
DATA = "https://data.alpaca.markets"

OBSERVE = int(os.environ.get("AGENT_OBSERVE", "5000"))
SHORTLIST = int(os.environ.get("AGENT_SHORTLIST", "500"))
MIN_PRICE = float(os.environ.get("AGENT_MIN_PRICE", "5"))
MAX_POSITION_PCT = float(os.environ.get("AGENT_MAX_POSITION_PCT", "10"))   # never suggest more than this % of the account in one stock
CASH_BUFFER_PCT = float(os.environ.get("AGENT_CASH_BUFFER_PCT", "10"))     # always leave this % of the account in cash
MAX_BUYS = int(os.environ.get("AGENT_MAX_BUYS", "12"))
WORKERS = int(os.environ.get("AGENT_WORKERS", "8"))
HORIZON = 5
LOG_KEEP = 3000
TM_LOOKBACK = int(os.environ.get("AGENT_TM_LOOKBACK", "250"))   # trading days of history the time machine replays
TM_MIN_SIGNALS = 6
TM_HIT_OK = 0.55
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}

HIGH_IMPACT = ["earnings", "guidance", "outlook", "downgrade", "upgrade", "sec ", "investigation", "lawsuit",
               "probe", "fraud", "recall", "fda", "approval", "merger", "acquire", "acquisition", "buyout",
               "bankrupt", "default", "ceo", "resign", "layoff", "tariff", "sanction", "halt", "short seller",
               "restat", "dividend cut", "split", "offering", "delist"]
MEDIUM_IMPACT = ["analyst", "price target", "rating", "partnership", "contract", "launch", "forecast",
                 "revenue", "profit", "loss", "deal", "stake", "buyback", "dividend", "beats", "misses"]

STATS = {}


# ---------------------------------------------------------------- HTTP + teams
def get(base, path, params=None, team=None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                if team:
                    STATS.setdefault(team, {"calls": 0})["calls"] += 1
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After") or 3) + attempt * 2)
                continue
            raise


def team_run(team, fn, jobs):
    """Run fn over jobs with WORKERS parallel workers; record the team's work."""
    t0 = time.time()
    s = STATS.setdefault(team, {"calls": 0})
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        out = list(ex.map(fn, jobs))
    s["jobs"] = s.get("jobs", 0) + len(jobs)
    s["seconds"] = round(s.get("seconds", 0) + time.time() - t0, 1)
    s["workers"] = WORKERS
    return out


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def crypto_pair(symbol):
    if "/" in symbol:
        return symbol
    for quote in ("USDT", "USDC", "USD", "BTC"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)] + "/" + quote
    return symbol


def iso(days_ago):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- data
def tradable_stocks():
    assets = get(TRADE, "/v2/assets", {"status": "active", "asset_class": "us_equity"}, "Scouts")
    return [a["symbol"] for a in assets
            if a.get("tradable") and a.get("exchange") in EXCHANGES and "/" not in a["symbol"]]


def stock_snapshots(symbols):
    def one(batch):
        d = get(DATA, "/v2/stocks/snapshots", {"symbols": ",".join(batch), "feed": "iex"}, "Scouts")
        return d.get("snapshots", d) if isinstance(d, dict) else {}
    out = {}
    for part in team_run("Scouts", one, chunks(symbols, 400)):
        out.update({k: v for k, v in part.items() if v})
    return out


def crypto_snapshots(symbols):
    if not symbols:
        return {}
    pairs = {crypto_pair(s): s for s in symbols}
    d = get(DATA, "/v1beta3/crypto/us/snapshots", {"symbols": ",".join(pairs)}, "Scouts")
    return {pairs.get(k, k): v for k, v in (d.get("snapshots") or {}).items()}


def bars(symbols, crypto, timeframe, days, team, per=50):
    def one(batch):
        res, token = {}, None
        while True:
            p = {"symbols": ",".join(crypto_pair(s) for s in batch) if crypto else ",".join(batch),
                 "timeframe": timeframe, "start": iso(days), "limit": 10000}
            if not crypto:
                p.update(feed="iex", adjustment="all")
            if token:
                p["page_token"] = token
            d = get(DATA, "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars", p, team)
            for s, b in (d.get("bars") or {}).items():
                res.setdefault(s.replace("/", "") if crypto else s, []).extend(b)
            token = d.get("next_page_token")
            if not token:
                return res
    out = {}
    if symbols:
        for part in team_run(team, one, chunks(symbols, per)):
            out.update(part)
    return out


def news(symbols, hours=72):
    def one(batch):
        res = {s.replace("/", ""): [] for s in batch}
        d = get(DATA, "/v1beta1/news", {"symbols": ",".join(res), "start": iso(hours / 24),
                                        "limit": 50, "sort": "desc"}, "News readers")
        for n in d.get("news") or []:
            item = {"headline": n.get("headline", "")[:220], "url": n.get("url"),
                    "source": n.get("source"), "created_at": n.get("created_at")}
            for s in n.get("symbols") or []:
                s = s.replace("/", "")
                if s in res and len(res[s]) < 10:
                    res[s].append(item)
        return res
    out = {}
    for part in team_run("News readers", one, chunks(symbols, 10)):
        out.update(part)
    return out


# ---------------------------------------------------------------- math helpers
def ema(xs, n):
    if not xs:
        return None
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    return 100.0 if losses == 0 else round(100 - 100 / (1 + gains / losses), 1)


def slope_pct(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = (n - 1) / 2, sum(xs) / n
    num = sum((i - mx) * (x - my) for i, x in enumerate(xs))
    den = sum((i - mx) ** 2 for i in range(n))
    return (num / den) / my * 100 if my else 0.0


def direction(v, eps):
    return "up" if v > eps else "down" if v < -eps else "flat"


def add_trading_days(d, n):
    while n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


# ---------------------------------------------------------------- check 1: data is real
def check_data(snap, position_price, daily, hourly, is_crypto):
    snap = snap or {}
    sources = []
    lt = (snap.get("latestTrade") or {}).get("p")
    if lt:
        sources.append(["Latest trade", float(lt)])
    q = snap.get("latestQuote") or {}
    bp, ap = q.get("bp") or 0, q.get("ap") or 0
    if bp > 0 and ap > 0 and (ap - bp) / ((ap + bp) / 2) < 0.02:
        sources.append(["Quote midpoint", (ap + bp) / 2])
    mb = (snap.get("minuteBar") or {}).get("c")
    if mb:
        sources.append(["Last minute bar", float(mb)])
    if position_price:
        sources.append(["Alpaca position", float(position_price)])
    if hourly:
        sources.append(["Last hourly bar", float(hourly[-1]["c"])])
    if daily:
        sources.append(["Last daily bar", float(daily[-1]["c"])])
    if not sources:
        return {"status": "alert", "summary": "No price came back from any source.", "sources": [], "timeframes": [], "issues": []}
    ref = sources[0][1]
    for s in sources:
        s.append(round((s[1] / ref - 1) * 100, 2) if ref else 0.0)
    spread = max(abs(s[2]) for s in sources)

    issues = []
    broken = [b for b in daily[-60:] if min(b["o"], b["h"], b["l"], b["c"]) <= 0
              or b["h"] < b["l"] or b["c"] > b["h"] * 1.0005 or b["c"] < b["l"] * 0.9995]
    if broken:
        issues.append(["alert", f"{len(broken)} daily bar{'s' if len(broken) > 1 else ''} with impossible prices (close outside high/low)."])
    if not is_crypto and len(daily) >= 2:
        days = [dt.date.fromisoformat(b["t"][:10]) for b in daily[-60:]]
        holes = sum(1 for a, b in zip(days, days[1:]) if (b - a).days > 5)
        if holes:
            issues.append(["watch", f"{holes} gap{'s' if holes > 1 else ''} of a week or more in the daily history."])
    if len(daily) < 40:
        issues.append(["watch", f"Only {len(daily)} days of history, too short to trust a trend."])
    jumps = [r for r in returns([b["c"] for b in daily[-60:]]) if abs(r) > 0.5]
    if jumps:
        issues.append(["watch", f"A {max(jumps, key=abs)*100:+.0f}% one-day jump. Could be a split or a bad print."])

    closes = [b["c"] for b in daily]
    hcl = [b["c"] for b in hourly]
    tfs = []
    if len(closes) >= 30:
        tfs.append(["Daily (EMA10 vs EMA30)", direction(ema(closes[-60:], 10) / ema(closes[-60:], 30) - 1, 0.004)])
    if len(closes) >= 21:
        tfs.append(["20-day change", direction(closes[-1] / closes[-21] - 1, 0.01)])
    if len(hcl) >= 20:
        tfs.append(["Hourly (last ~5 days)", direction(slope_pct(hcl[-35:]), 0.02)])
    dirs = [d for _, d in tfs]
    agree = max((dirs.count(x) for x in ("up", "down", "flat")), default=0)

    if spread > 2 or any(i[0] == "alert" for i in issues):
        status = "alert"
        summary = (f"Prices disagree by {spread:.2f}% across sources." if spread > 2 else "The price history has broken bars.") + " Don't trust this data."
    elif spread > 0.75 or issues or (len(dirs) == 3 and agree == 1):
        status = "watch"
        summary = f"Prices agree within {spread:.2f}%, but read the notes below." if spread <= 0.75 else f"Prices differ by {spread:.2f}% across sources."
    else:
        status = "ok"
        summary = f"Real data: {len(sources)} sources agree within {spread:.2f}%, history is clean, {agree} of {len(dirs)} timeframes agree."
    return {"status": status, "summary": summary, "spread_pct": round(spread, 2),
            "sources": [{"name": n, "price": round(p, 4), "diff_pct": d} for n, p, d in sources],
            "issues": [{"level": l, "text": t} for l, t in issues],
            "timeframes": [{"name": n, "trend": d} for n, d in tfs], "agree": agree, "of": len(dirs)}


# ---------------------------------------------------------------- check 2: organic trend
def check_organic(daily, hourly, headline_count, is_crypto):
    if len(daily) < 22:
        return {"status": "watch", "score": None, "summary": "Not enough history to judge this trend yet.", "flags": []}
    closes = [b["c"] for b in daily]
    vols = [b.get("v") or 0 for b in daily]
    rets = returns(closes)
    sd = st.pstdev(rets[-60:]) or 1e-9
    med_vol = st.median(vols[-21:-1]) or 1e-9
    last_ret, vol_ratio = rets[-1], vols[-1] / med_vol
    flags, score = [], 100

    if vol_ratio >= 3 and abs(last_ret) >= 2 * sd:
        if headline_count:
            flags.append(["info", f"Volume {vol_ratio:.1f}× normal with a {last_ret*100:+.1f}% move, and there is news behind it."])
            score -= 5
        elif headline_count is None:
            flags.append(["watch", f"Volume {vol_ratio:.1f}× normal with a {last_ret*100:+.1f}% move. News not checked yet."])
            score -= 25
        else:
            flags.append(["alert", f"Volume {vol_ratio:.1f}× normal and a {last_ret*100:+.1f}% move with no news found. Classic pump pattern."])
            score -= 40
    elif vol_ratio >= 3:
        flags.append(["watch", f"Volume {vol_ratio:.1f}× normal today without a big price move."])
        score -= 10

    move10 = closes[-1] / closes[-11] - 1
    biggest = max(rets[-10:], key=abs)
    if abs(move10) > 3 * sd and abs(biggest) / abs(move10) > 0.7 and (biggest > 0) == (move10 > 0):
        flags.append(["watch", f"One day ({biggest*100:+.1f}%) did {abs(biggest)/abs(move10)*100:.0f}% of the 10-day move. The trend rests on a single jump."])
        score -= 20

    gaps = [abs(daily[i]["o"] / daily[i - 1]["c"] - 1) for i in range(len(daily) - 5, len(daily)) if daily[i - 1]["c"]]
    if not is_crypto and gaps and max(gaps) > max(0.04, 3 * sd):
        flags.append(["watch", f"Price gapped {max(gaps)*100:.1f}% at an open in the last week."])
        score -= 10

    if len(hourly) >= 12:
        h = [b["c"] for b in hourly[-24:]]
        lo0, peak = h[0], max(h)
        rise = peak / lo0 - 1 if lo0 else 0
        if rise > max(0.03, 2 * sd) and peak > lo0 and (peak - h[-1]) / (peak - lo0) > 0.6:
            flags.append(["alert", f"Rose {rise*100:.1f}% then gave back most of it within hours. Looks like pump-and-fade."])
            score -= 30

    dollar_vol = st.mean(c * v for c, v in zip(closes[-20:], vols[-20:]))
    if not is_crypto and dollar_vol < 1_000_000:
        flags.append(["watch", f"Thin trading: about ${dollar_vol/1e6:.2f}M a day on the IEX feed. Thin stocks are easier to push around."])
        score -= 15

    score = max(0, score)
    status = "alert" if any(f[0] == "alert" for f in flags) or score < 50 else "watch" if score < 80 else "ok"
    summary = {"ok": "Moves look like ordinary trading. Volume and price behave normally.",
               "watch": "Something is unusual. Read the flags below.",
               "alert": "This trend shows signs of being manufactured. Be careful."}[status]
    return {"status": status, "score": score, "summary": summary,
            "flags": [{"level": l, "text": t} for l, t in flags],
            "stats": {"volume_vs_normal": round(vol_ratio, 2), "move_today_pct": round(last_ret * 100, 2),
                      "daily_vol_pct": round(sd * 100, 2), "avg_dollar_volume": round(dollar_vol)}}


# ---------------------------------------------------------------- check 3: prediction
def predict(sym, daily, today):
    closes = [b["c"] for b in daily]
    if len(closes) < 40:
        return None
    sd = st.pstdev(returns(closes)[-60:]) or 1e-9
    price = closes[-1]
    e10, e30 = ema(closes[-90:], 10), ema(closes[-90:], 30)
    z = (e10 / e30 - 1) / (sd * math.sqrt(10))
    r = rsi(closes)
    lean = "up" if z > 0.35 else "down" if z < -0.35 else "flat"
    reasons = [f"EMA10 is {(e10/e30-1)*100:+.2f}% vs EMA30 (z {z:+.2f})."]
    if r is not None and r > 72 and lean == "up":
        lean, reasons = "flat", reasons + [f"RSI {r} is stretched, so upside is likely capped."]
    elif r is not None and r < 28 and lean == "down":
        lean, reasons = "flat", reasons + [f"RSI {r} is oversold, so downside is likely capped."]
    elif r is not None:
        reasons.append(f"RSI {r}.")
    drift = {"up": 0.4, "down": -0.4, "flat": 0.0}[lean] * sd * math.sqrt(HORIZON)
    band = sd * math.sqrt(HORIZON)
    conf = round(min(0.7, 0.5 + abs(z) * 0.08), 2) if lean != "flat" else 0.5
    return {"symbol": sym, "made_on": today.isoformat(), "due": add_trading_days(today, HORIZON).isoformat(),
            "start": round(price, 4), "call": lean, "target": round(price * (1 + drift), 4),
            "low": round(price * (1 + drift - band), 4), "high": round(price * (1 + drift + band), 4),
            "band_pct": round(band * 100, 2), "confidence": conf, "reasons": reasons}


def score_prediction(p, daily):
    after = [b["c"] for b in daily if b["t"][:10] >= p["due"]]
    if not after:
        return p
    end, band = after[0], p["band_pct"] / 100
    move = end / p["start"] - 1
    actual = "up" if move > band * 0.25 else "down" if move < -band * 0.25 else "flat"
    return dict(p, end=round(end, 4), move_pct=round(move * 100, 2), actual=actual,
                hit=actual == p["call"], in_range=p["low"] <= end <= p["high"])


def prediction_check(pred, organic_status):
    if not pred:
        return {"status": "watch", "summary": "Not enough history for a prediction yet."}
    status = {"up": "ok", "flat": "watch", "down": "alert"}[pred["call"]]
    summary = f"Leans {pred['call']} over {HORIZON} trading days, expected range {pred['low']:.2f} to {pred['high']:.2f}."
    if organic_status == "alert" and status == "ok":
        status, summary = "watch", summary + " Low trust: check 2 flagged this trend."
    return dict(pred, status=status, summary=summary)


# ---------------------------------------------------------------- check 4: news
def check_news(items, move_today):
    if items is None:
        return {"status": "skip", "summary": "Not read: news is checked only for stocks that clear checks 1-3, and for holdings.", "headlines": [], "count": None}
    if not items:
        return {"status": "ok", "level": "quiet", "summary": "No headlines in the last 72 hours.", "headlines": [], "count": 0}
    scored = []
    for n in items:
        h = (n["headline"] or "").lower() + " "
        hi = [w.strip() for w in HIGH_IMPACT if w in h]
        md = [w for w in MEDIUM_IMPACT if w in h]
        scored.append(dict(n, impact="high" if hi else "medium" if md else "low", keywords=(hi or md)[:3]))
    highs = sum(1 for s in scored if s["impact"] == "high")
    if highs and abs(move_today or 0) >= 3:
        status, level = "alert", "hot"
        summary = f"{highs} high-impact headline{'s' if highs > 1 else ''} and a {move_today:+.1f}% move today. The news is moving the price."
    elif highs or len(items) >= 6:
        status, level = "watch", "active"
        summary = f"{len(items)} headlines, {highs} high-impact. Could move the price; check before trading."
    else:
        status, level = "ok", "low"
        summary = f"{len(items)} headline{'s' if len(items) > 1 else ''}, nothing that usually moves a stock much."
    scored.sort(key=lambda s: {"high": 0, "medium": 1, "low": 2}[s["impact"]])
    return {"status": status, "level": level, "summary": summary, "count": len(items), "high": highs, "headlines": scored[:5]}


# ---------------------------------------------------------------- check 5: time machine
_PAST_DAY = dt.date(2000, 1, 3)


def replay(daily, i, crypto):
    """Checks 2 and 3 exactly as they would have read at the close of bar i, using only bars up to i.
    News from the past isn't replayed, so a volume spike counts as unexplained (watch, not green)."""
    if i < 60:
        return None, None
    win = daily[max(0, i - 100):i + 1]
    org = check_organic(win, [], None, crypto)["status"]
    p = predict("", win, _PAST_DAY)
    return org, (p["call"] if p else None)


def replay_series(daily, crypto, start=60):
    """[(date, green_then, forward_5d_return or None)] for every bar from `start`."""
    out = []
    for i in range(max(60, start), len(daily)):
        org, call = replay(daily, i, crypto)
        fwd = daily[i + HORIZON]["c"] / daily[i]["c"] - 1 if i + HORIZON < len(daily) and daily[i]["c"] else None
        out.append((daily[i]["t"][:10], org == "ok" and call == "up", fwd, org, call))
    return out


def judge_history(signals, lookback_days):
    """signals: [(date, forward_return)] for past green days whose outcome was already known."""
    n = len(signals)
    if n < TM_MIN_SIGNALS:
        return {"status": "watch", "n": n, "hit_rate": None, "avg_fwd_pct": None, "lookback_days": lookback_days,
                "summary": f"Only {n} green day{'s' if n != 1 else ''} in the past {lookback_days} trading days. Too few to trust.",
                "recent": [[d, round(f * 100, 2)] for d, f in signals[-6:]]}
    hits = sum(1 for _, f in signals if f > 0)
    hit, avg = hits / n, sum(f for _, f in signals) / n
    if hit >= TM_HIT_OK and avg > 0:
        status = "ok"
        summary = f"When this stock looked green before, it rose over the next 5 days {hits} of {n} times ({hit:.0%}), {avg*100:+.2f}% on average."
    elif avg < 0 and hit < 0.5:
        status = "alert"
        summary = f"When this stock looked green before, it usually fell: up only {hits} of {n} times ({hit:.0%}), {avg*100:+.2f}% on average."
    else:
        status = "watch"
        summary = f"Mixed past: up {hits} of {n} times ({hit:.0%}) after green days, {avg*100:+.2f}% on average."
    best, worst = max(signals, key=lambda x: x[1]), min(signals, key=lambda x: x[1])
    return {"status": status, "n": n, "hits": hits, "hit_rate": round(hit, 3), "avg_fwd_pct": round(avg * 100, 2),
            "best": [best[0], round(best[1] * 100, 2)], "worst": [worst[0], round(worst[1] * 100, 2)],
            "lookback_days": lookback_days, "summary": summary, "recent": [[d, round(f * 100, 2)] for d, f in signals[-6:]]}


def check_time_machine(daily, crypto):
    if len(daily) < 60 + HORIZON + TM_MIN_SIGNALS:
        return {"status": "watch", "n": 0, "summary": f"Only {len(daily)} days of price history. The time machine needs more.",
                "lookback_days": 0, "recent": []}
    start = max(60, len(daily) - TM_LOOKBACK)
    ser = replay_series(daily, crypto, start)
    sig = [(d, f) for d, g, f, _, _ in ser if g and f is not None]
    return judge_history(sig, len(ser))


def time_machine_all(stocks, cryptos, cache_path, day_key):
    """Check 5 for every symbol, cached per New York date (the past doesn't change during the day)."""
    cache = {}
    if cache_path:
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    if cache.get("date") != day_key or cache.get("lookback") != TM_LOOKBACK:
        cache = {"date": day_key, "lookback": TM_LOOKBACK, "results": {}}
    res = cache["results"]
    need_s = [s for s in stocks if s not in res]
    need_c = [s for s in cryptos if s not in res]
    t0 = time.time()
    days = int(TM_LOOKBACK * 1.45) + 170
    if need_s or need_c:
        long = bars(need_s, False, "1Day", days, "Time machine", per=25)
        long.update(bars(need_c, True, "1Day", days, "Time machine"))
        for s in need_s + need_c:
            res[s] = check_time_machine(long.get(s, []), s in need_c)
    st_ = STATS.setdefault("Time machine", {"calls": 0})
    st_.update(jobs=len(need_s) + len(need_c), workers=WORKERS, seconds=round(time.time() - t0, 1),
               cached=len(stocks) + len(cryptos) - len(need_s) - len(need_c))
    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        json.dump(cache, open(cache_path, "w"), separators=(",", ":"))
    return res


# ---------------------------------------------------------------- advisor
# statuses are always in this order: data (1), organic (2), prediction (3), news (4), time machine (5)
def signal(statuses, held):
    greens = statuses.count("ok")
    if held:
        if statuses[4] == "alert" or greens <= 2:
            return "sell"
        return "start_selling" if greens == 3 else "hold"
    vetoes = statuses[0] == "ok" and statuses[4] == "ok"
    if greens >= 4 and vetoes and "alert" not in statuses:
        return "buy"
    return "watch" if greens >= 3 else None


def can_still_buy(statuses):
    """Worth reading the news for: vetoes green, nothing red so far, and organic or prediction green."""
    s0, s1, s2, s4 = statuses[0], statuses[1], statuses[2], statuses[4]
    return s0 == "ok" and s4 == "ok" and "alert" not in (s1, s2) and "ok" in (s1, s2)


def verdict(statuses):
    if "alert" in statuses:
        return "red_flag"
    return "verified" if statuses.count("ok") == 5 else "caution"


# ---------------------------------------------------------------- run
def load_previous(src):
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


def run(portfolio, previous, watchlist, tm_cache=None):
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    STATS.clear()
    positions = {p["symbol"]: p for p in portfolio.get("positions", [])}
    account = portfolio.get("account") or {}
    held_crypto = [s for s, p in positions.items() if p.get("asset_class") == "crypto"]
    held_stocks = [s for s in positions if s not in held_crypto]

    # Scouts: the whole tradable market
    t0 = time.time()
    universe = tradable_stocks()
    snaps = stock_snapshots(sorted(set(universe + held_stocks + watchlist)))
    snaps.update(crypto_snapshots(held_crypto))
    STATS["Scouts"]["seconds"] = round(time.time() - t0, 1)

    def price_of(s):
        sn = snaps.get(s) or {}
        return (sn.get("latestTrade") or {}).get("p") or (sn.get("dailyBar") or {}).get("c") or 0

    def dollar_vol(s):
        sn = snaps.get(s) or {}
        b = sn.get("prevDailyBar") or sn.get("dailyBar") or {}
        return (b.get("c") or 0) * (b.get("v") or 0)

    def change(s):
        prev = ((snaps.get(s) or {}).get("prevDailyBar") or {}).get("c")
        return (price_of(s) / prev - 1) * 100 if prev else None

    priced = [s for s in universe if price_of(s) > 0 and dollar_vol(s) > 0]
    observed = sorted(priced, key=dollar_vol, reverse=True)[:OBSERVE]

    # Screeners: shortlist + world movers from the scan
    t0 = time.time()
    shortlist = [s for s in observed if price_of(s) >= MIN_PRICE][:SHORTLIST]
    extra = [s for s in dict.fromkeys(held_stocks + watchlist) if s not in shortlist]
    mv = sorted([s for s in observed[:3000] if price_of(s) >= MIN_PRICE and change(s) is not None], key=change)
    row = lambda s: {"symbol": s, "price": round(price_of(s), 2), "change_pct": round(change(s), 2)}
    movers = {"up": [row(s) for s in mv[::-1][:8]], "down": [row(s) for s in mv[:8]]}
    STATS["Screeners"] = {"calls": 0, "jobs": len(observed), "workers": 1, "seconds": round(time.time() - t0, 2)}

    # Verifiers: price history, then checks 1-3
    check_stocks = shortlist + extra
    daily = bars(check_stocks, False, "1Day", 130, "Verifiers", per=40)
    hourly = bars(check_stocks, False, "1Hour", 10, "Verifiers", per=60)
    daily.update(bars(held_crypto, True, "1Day", 130, "Verifiers"))
    hourly.update(bars(held_crypto, True, "1Hour", 10, "Verifiers"))

    # Time machine (check 5): each stock's past year, cached for the day
    hist = time_machine_all(check_stocks, held_crypto, tm_cache, now.astimezone(dt.timezone(dt.timedelta(hours=-4))).date().isoformat())

    rows = {}
    for s in check_stocks + held_crypto:
        d, h = daily.get(s, []), hourly.get(s, [])
        crypto = s in held_crypto
        pos = positions.get(s)
        c1 = check_data(snaps.get(s), pos and pos.get("price"), d, h, crypto)
        c2 = check_organic(d, h, None, crypto)
        pred = predict(s, d, today)
        ch = change(s)
        move = round((d[-1]["c"] / d[-2]["c"] - 1) * 100, 2) if len(d) >= 2 else (round(ch, 2) if ch is not None else None)
        rows[s] = {"symbol": s, "held": bool(pos), "watch": s in watchlist, "crypto": crypto,
                   "price": round(price_of(s) or (d[-1]["c"] if d else 0), 4), "move_today_pct": move,
                   "dollar_volume": round(dollar_vol(s)), "c": [c1, c2, prediction_check(pred, c2["status"]), None,
                                                             hist.get(s) or {"status": "watch", "summary": "No history available."}],
                   "pred": pred}

    # News readers: only holdings, watchlist, and stocks that can still reach a buy
    finalists = [s for s, r in rows.items() if r["held"] or r["watch"]
                 or can_still_buy([c["status"] if c else None for c in r["c"]])]
    heads = news(finalists) if finalists else {}
    for s, r in rows.items():
        if s not in finalists:
            r["c"][3] = check_news(None, r["move_today_pct"])
            continue
        items = heads.get(s.replace("/", ""), [])
        r["c"][3] = check_news(items, r["move_today_pct"])
        d, h = daily.get(s, []), hourly.get(s, [])
        r["c"][1] = check_organic(d, h, len(items), r["crypto"])   # re-judge volume now that news is known
        r["c"][2] = prediction_check(r["pred"], r["c"][1]["status"])

    # Advisor: green flags -> signals, sized buys
    for r in rows.values():
        sts = [c["status"] for c in r["c"]]
        r["greens"] = sts.count("ok")
        r["verdict"] = verdict(sts)
        r["signal"] = signal(sts, r["held"])
    equity = float(account.get("equity") or 0)
    cash = float(account.get("cash") or 0)
    spendable = max(0.0, cash - equity * CASH_BUFFER_PCT / 100)
    buys = sorted([r for r in rows.values() if r["signal"] == "buy"],
                  key=lambda r: (r["c"][1].get("score") or 0) + r["c"][2].get("confidence", 0.5) * 100
                  + (r["c"][4].get("hit_rate") or 0) * 100 + r["greens"] * 10
                  + min(10, math.log10(max(r["dollar_volume"], 1))), reverse=True)
    for i, r in enumerate(buys):
        r["rank"] = i + 1
    top = buys[:MAX_BUYS]
    per_buy = spendable / min(len(top), 5) if top else 0
    cap = equity * MAX_POSITION_PCT / 100
    for r in top:
        r["size"] = {"max_usd": math.floor(max(0.0, min(cap, per_buy)) / 10) * 10, "cap_usd": round(cap, 2),
                     "spendable_usd": round(spendable, 2),
                     "rule": f"at most {MAX_POSITION_PCT:g}% of the account in one stock, keep {CASH_BUFFER_PCT:g}% cash"}

    # Prediction log: holdings, top buys and 3-green stocks
    log = [score_prediction(p, daily.get(p["symbol"], [])) if "hit" not in p else p for p in previous.get("log", [])]
    detailed = [r for r in rows.values() if r["held"] or r["watch"] or r in top or r["signal"] == "watch"][:80]
    seen = {(p["symbol"], p["made_on"]) for p in log}
    for r in detailed:
        p = r["pred"]
        if p and (p["symbol"], p["made_on"]) not in seen:
            log.append({k: p[k] for k in ("symbol", "made_on", "due", "start", "call", "target", "low", "high", "band_pct", "confidence")})
    log = log[-LOG_KEEP:]
    scored = [p for p in log if "hit" in p]

    def full(r):
        mine = [p for p in scored if p["symbol"] == r["symbol"]]
        c3 = dict(r["c"][2], track={"scored": len(mine), "hits": sum(p["hit"] for p in mine)})
        d = daily.get(r["symbol"], [])[-60:]
        out = {k: r.get(k) for k in ("symbol", "held", "watch", "price", "move_today_pct", "greens", "verdict", "signal", "size", "rank")}
        out.update(checks={"price": r["c"][0], "organic": r["c"][1], "prediction": c3, "news": r["c"][3], "history": r["c"][4]},
                   closes=[round(b["c"], 4) for b in d], dates=[b["t"][:10] for b in d])
        return out

    order = {"sell": 0, "start_selling": 1, "buy": 2, "hold": 3, "watch": 4, None: 5}
    board = sorted(rows.values(), key=lambda r: (order[r["signal"]], -r["greens"], -r["dollar_volume"]))
    names = ("price", "organic", "prediction", "news", "history")
    return {
        "generated_at": now.isoformat(timespec="seconds"), "horizon_days": HORIZON,
        "hierarchy": "vetoes: time machine + data is real; then 2 of organic, prediction, news; buy at 4+/5 with no red",
        "rules": {"observe": OBSERVE, "shortlist": SHORTLIST, "min_price": MIN_PRICE, "max_position_pct": MAX_POSITION_PCT,
                  "tm_lookback_days": TM_LOOKBACK, "tm_min_signals": TM_MIN_SIGNALS, "tm_hit_ok": TM_HIT_OK,
                  "cash_buffer_pct": CASH_BUFFER_PCT, "max_buys": MAX_BUYS},
        "funnel": {"tradable": len(universe), "observed": len(observed), "shortlisted": len(shortlist),
                   "checked": len(rows), "news_read": len(finalists),
                   "greens": {str(g): sum(1 for r in rows.values() if r["greens"] == g) for g in range(6)},
                   "failed": {k: sum(1 for r in rows.values() if r["c"][i]["status"] == "alert") for i, k in enumerate(names)},
                   "passed": {k: sum(1 for r in rows.values() if r["c"][i]["status"] == "ok") for i, k in enumerate(names)},
                   **{k: sum(1 for r in rows.values() if r["signal"] == k) for k in ("buy", "hold", "start_selling", "sell")}},
        "teams": [dict({"name": n, "job": j}, **STATS.get(n, {})) for n, j in (
            ("Scouts", "Snapshot every tradable US stock"), ("Screeners", "Rank by dollar volume, shortlist"),
            ("Verifiers", "Price history and checks 1-3"), ("Time machine", "Replays each stock's past year (check 5)"),
            ("News readers", "Check 4 for finalists"))],
        "movers": movers,
        "symbols": [full(r) for r in sorted(detailed, key=lambda r: (order[r["signal"]], -r["greens"]))],
        "board_columns": ["symbol", "price", "move_today_pct", "price_check", "organic", "prediction", "news", "history",
                          "greens", "organic_score", "held", "signal", "tm_hit_rate", "tm_n"],
        "board": [[r["symbol"], r["price"], r["move_today_pct"], *[c["status"] for c in r["c"]], r["greens"],
                   r["c"][1].get("score"), int(r["held"]), r["signal"], r["c"][4].get("hit_rate"), r["c"][4].get("n")] for r in board],
        "track_record": {"scored": len(scored), "hits": sum(p["hit"] for p in scored),
                         "in_range": sum(p.get("in_range", False) for p in scored)},
        "log": log,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out")
    ap.add_argument("--portfolio", required=True)
    ap.add_argument("--previous", help="last published agent.json (URL or path), for the prediction log")
    ap.add_argument("--watchlist", default=os.environ.get("AGENT_WATCHLIST", ""))
    ap.add_argument("--tm-cache", help="where to keep the day's time-machine results")
    a = ap.parse_args()
    previous = load_previous(a.previous)
    try:
        with open(a.portfolio) as f:
            portfolio = json.load(f)
    except Exception:
        portfolio = {}
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not (KEY and SECRET):
        out = {"generated_at": stamp, "configured": False, "symbols": [], "log": previous.get("log", [])}
    else:
        try:
            out = run(portfolio, previous, [s.strip().upper() for s in a.watchlist.split(",") if s.strip()], a.tm_cache)
            out["configured"] = True
        except Exception as e:  # keep the site working and the log intact
            out = dict(previous, generated_at=stamp, configured=True, error=f"{type(e).__name__}: {e}"[:300])
            out.setdefault("symbols", [])
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    fn = out.get("funnel", {})
    print(f"wrote {a.out}: tradable={fn.get('tradable')} observed={fn.get('observed')} shortlisted={fn.get('shortlisted')} "
          f"buy={fn.get('buy')} sell={fn.get('sell')} log={len(out.get('log', []))} error={out.get('error')}")


if __name__ == "__main__":
    main()
