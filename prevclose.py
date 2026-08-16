# ─────────────────────────────────────────────────────────────────────────────
#  prevclose.py  —  yesterday's close for every stock, which is what every
#                   colour on the page actually depends on
#
#      change% = (LTP - prev_close) / prev_close * 100
#
#  Get prev_close wrong and the whole heatmap is wrong in a way that still looks
#  plausible, so this module does not guess.  Measured against the live API on
#  2026-08-10:
#
#    ✗ quote.net_change      0 for every stock.  Dhan does not populate it.
#    ✗ the WS Prev-Close packet (code 6) never arrived on a Quote subscription —
#      only Quote packets (code 4) came back.
#    ~ quote.ohlc.close      AFTER the close this is TODAY's close (it equalled
#      last_price for all six stocks sampled).  During the session it should be
#      yesterday's — that is the exchange convention — but "should be" is not a
#      thing to hang every colour on.
#    ✓ /v2/charts/historical daily bars end at the LAST COMPLETED SESSION.  On
#      2026-08-10 (a Monday, post-close) the newest bar was Friday 2026-08-07 —
#      today's bar was absent even after the close.  Exactly what is needed.
#
#  So: take the cheap bulk field, VERIFY it against the expensive-but-certain
#  one on a sample, and only trust it if they agree.
#
#      1. cache      _prevclose_<day>.json — a restart mid-session costs nothing
#      2. bulk       ONE /v2/marketfeed/quote for all ~210 stocks
#      3. verify     config.PREVCLOSE_SAMPLE stocks against daily history
#      4. fallback   if they disagree, pull daily history for EVERY stock
#
#  Step 3 is what makes step 4 self-selecting.  Run this during market hours and
#  the bulk field is yesterday's close, the sample agrees, and the whole thing
#  costs one request.  Run it after 15:30 and the bulk field has flipped to
#  today's close, the sample disagrees, and it falls through to the slow path —
#  which is correct, because after the close "today's change" still means
#  today's close against yesterday's.
#
#  ── Why the slow path is paced ──────────────────────────────────────────────
#  /v2/charts/historical rate-limits hard.  Measured: 5 concurrent workers gave
#  3 x HTTP 429 (DH-904) out of 20, and even 3 workers at a 0.34s gap needed 23
#  retries for 30 stocks.  So the pacer below starts at REQ_GAP and WIDENS it on
#  every 429, which finds the machine's real ceiling instead of assuming one.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import date, datetime, timedelta

import config
import dhan_feed

QUOTE_URL = "https://api.dhan.co/v2/marketfeed/quote"
HIST_URL = "https://api.dhan.co/v2/charts/historical"

QUOTE_BATCH = 1000       # Dhan's documented per-request instrument cap
HIST_WORKERS = 3
REQ_GAP = 0.34           # seconds between historical requests, adaptive
MAX_GAP = 1.60
HIST_RETRIES = 5

#  How close the bulk value must be to the daily-history value to count as the
#  same number.  A tick is 0.05 paise on a 4-figure stock, so 0.05% is loose
#  enough for rounding and far tighter than a real day's move.
MATCH_TOL_PCT = 0.05


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _source() -> str:
    """config.PREVCLOSE_SOURCE — 'db' or 'quote'.  Anything else means quote."""
    return "db" if str(getattr(config, "PREVCLOSE_SOURCE", "quote")
                       ).lower() == "db" else "quote"


def expected_prev_day(day: "date | None" = None) -> date:
    """
    The session `prev_close` MUST come from — the last trading day before
    `day`.  Weekends and NSE holidays come from nse_holidays.

    ── Why this is checked and not inferred ────────────────────────────────
    The rule used to be "the newest daily bar before today", which quietly
    assumes the history API is current.  It is not: on 2026-08-10 at 19:47,
    hours after the close, /v2/charts/historical still ended at 2026-08-07 —
    Monday's daily bar had not been published.  A run at 00:47 on 2026-08-11
    therefore took FRIDAY's close as "yesterday", and every tile on the map
    compared today's price against a session two back.  RELIANCE showed
    prev 1334.8 (Aug 7) instead of 1327.3 (Aug 10); PAYTM read +11% off a
    close that was two days stale.

    Nothing about that looks wrong on screen, which is exactly why the day is
    now demanded explicitly instead of being whatever the API last had.

    `day` is the SESSION day, not the calendar day — on a Sunday it is Friday.
    Defaulting it to date.today() was the 2026-08-16 bug: Sunday's
    last_trading_day is Friday, and the screen was showing Friday too, so every
    tile read +0.00%.  See nse_holidays.session_day().
    """
    import nse_holidays
    return nse_holidays.last_trading_day(
        day or nse_holidays.session_day())


#  Today's session is "settled" from here on — the same 15:40 db_update uses.
SESSION_DONE_MIN = 15 * 60 + 40


def quote_gives_prev_session(day: "date | None" = None,
                             now: "datetime | None" = None
                             ) -> "tuple[bool, str]":
    """
    Is `marketfeed/quote`'s ohlc.close the PREVIOUS session's close right now?

    That field always holds the last SETTLED session's close.  `day` is the
    session ON SCREEN, so the field is the PREVIOUS session's close only while
    that session is still running:

        today, before 15:40  ->  YES   (Tue 09:00 gives Monday's close)
        today, after  15:40  ->  no    (it has flipped to today's)
        a PAST session       ->  no    (Sunday shows Friday, and the field IS
                                        Friday's close — its own, not its
                                        previous)

    That last row used to read YES, because `day` was the calendar day and a
    weekend was simply "not a trading day".  Under the session framing it is
    wrong: on 2026-08-16 the screen showed Friday and the field held Friday.

    This is decided from the clock and the calendar, not by sampling.  The old
    code asked the history API what it thought and followed that — and the
    history API is a session behind for hours after the close, so a 00:47 run
    took Friday's close as "yesterday" and every tile on the map was priced off
    a session two back.  Measured: RELIANCE prev 1334.8 (Aug 7) instead of
    1327.3 (Aug 10), PAYTM reading +11% off a stale base.
    """
    import nse_holidays
    now = now or datetime.now()
    day = day or nse_holidays.session_day(now.date())
    #  A session in the PAST has settled no matter what the clock says today.
    #  Without this, a Sunday-10:00 run would read the 10:00 against Friday's
    #  session and answer "abhi settle nahi hua" — two days after it closed.
    if day < now.date():
        return False, f"{day} ka session khatam ho chuka — quote ka close USI " \
                      f"din ka hai, uska pichhla nahi"
    if not nse_holidays.is_trading_day(day):
        return True, f"{day} trading day nahi hai — quote ka close pichhle " \
                     f"session ka hi hai"
    mins = now.hour * 60 + now.minute
    if mins < SESSION_DONE_MIN:
        return True, f"aaj ka session abhi settle nahi hua ({now:%H:%M} < " \
                     f"15:40) — quote ka close pichhle session ka hai"
    return False, f"aaj ka session settle ho chuka ({now:%H:%M} >= 15:40) — " \
                  f"quote ka close AAJ ka ban chuka hai, use nahi kar sakte"


def db_prev_close(sids: "list[str]", want: date) -> "dict[str, float]":
    """
    `want` ke din ka aakhri 5-minute bar ka close, local DB se.

    Not the official 15:30 close — that DB's last bar is 15:10 — so this is a
    FALLBACK, not a source of record.  What it does have is the right SESSION,
    instantly and without an API, which is what matters when the history API is
    lagging and the quote field has already flipped to today's close.
    """
    import sqlite3
    out: "dict[str, float]" = {}
    try:
        con = sqlite3.connect(f"file:{config.FIVEMIN_DB}?mode=ro", uri=True,
                              timeout=60)
    except Exception:
        return out
    try:
        lo, hi = f"{want.isoformat()} 00:00:00", f"{want.isoformat()} 23:59:59"
        for sid in sids:
            try:
                row = con.execute(
                    f'SELECT close FROM '
                    f'"{config.FIVEMIN_TABLE.format(sid=sid)}" '
                    f"WHERE date BETWEEN ? AND ? ORDER BY date DESC LIMIT 1",
                    (lo, hi)).fetchone()
            except Exception:
                continue
            if row and row[0]:
                out[sid] = float(row[0])
    finally:
        con.close()
    return out


#  The pooled, retrying session lives in dhan_feed so the preflight and these
#  calls share one keep-alive pool — see the note there on this machine's TLS
#  resets, which is the reason any of this is pooled at all.
session = dhan_feed.session


class _Pacer:
    """One global gap between historical requests, widened whenever Dhan 429s."""

    def __init__(self, gap: float = REQ_GAP):
        self.gap = gap
        self._lock = threading.Lock()
        self._last = 0.0
        self.throttled = 0

    def wait(self):
        with self._lock:
            left = self._last + self.gap - time.time()
            if left > 0:
                time.sleep(left)
            self._last = time.time()

    def back_off(self):
        with self._lock:
            self.throttled += 1
            self.gap = min(self.gap + 0.12, MAX_GAP)


# ── The two sources ──────────────────────────────────────────────────────────

def bulk_quote(cid: str, token: str,
               sids: "list[str]") -> "dict[str, dict]":
    """
    One /v2/marketfeed/quote per 1000 stocks.

    Returns sid -> {"close", "ltp", "open", "high", "low", "volume"}.  `close`
    is the field under test; `ltp` is used to paint tiles before the first tick
    arrives, which is what makes the page useful outside market hours.
    """
    out: "dict[str, dict]" = {}
    hdrs = dhan_feed.headers(cid, token)
    for i in range(0, len(sids), QUOTE_BATCH):
        chunk = [int(s) for s in sids[i:i + QUOTE_BATCH]]
        body = json.dumps({"NSE_EQ": chunk})
        for attempt in range(4):
            try:
                r = session().post(QUOTE_URL, headers=hdrs, data=body, timeout=30)
            except Exception:
                time.sleep(2)            # this machine resets the odd TLS conn
                continue
            if r.status_code != 200:
                time.sleep(2)
                continue
            seg = (r.json().get("data") or {}).get("NSE_EQ") or {}
            for sid, v in seg.items():
                ohlc = v.get("ohlc") or {}
                out[str(sid)] = {
                    "close": _f(ohlc.get("close")),
                    "open": _f(ohlc.get("open")),
                    "high": _f(ohlc.get("high")),
                    "low": _f(ohlc.get("low")),
                    "ltp": _f(v.get("last_price")),
                    "volume": v.get("volume") or 0,
                }
            break
    return out


def daily_prev_close(cid: str, token: str, sid: str,
                     pacer: "_Pacer") -> "tuple[float, str] | None":
    """
    (close, 'YYYY-MM-DD') of the newest daily bar STRICTLY BEFORE today.

    The API already stops at the last completed session, but the `< today`
    filter is kept anyway: if Dhan ever starts publishing an in-progress daily
    bar, taking the last row blindly would silently compare today against
    itself and paint the entire map flat.
    """
    import nse_holidays
    today = nse_holidays.session_day()
    body = json.dumps({
        "securityId": str(sid), "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY", "expiryCode": 0, "oi": False,
        #  20 calendar days always spans a previous session, even across a long
        #  weekend plus a couple of NSE holidays.
        "fromDate": (today - timedelta(days=20)).isoformat(),
        "toDate": today.isoformat(),
    })
    hdrs = dhan_feed.headers(cid, token)
    for _ in range(HIST_RETRIES):
        pacer.wait()
        try:
            r = session().post(HIST_URL, headers=hdrs, data=body, timeout=25)
        except Exception:
            time.sleep(1.0)
            continue
        if r.status_code == 429:
            pacer.back_off()
            time.sleep(1.2)
            continue
        if r.status_code != 200:
            time.sleep(1.0)
            continue
        j = r.json()
        closes, stamps = j.get("close") or [], j.get("timestamp") or []
        for c, ts in zip(reversed(closes), reversed(stamps)):
            d = datetime.fromtimestamp(ts).date()
            if d < today and c:
                return float(c), d.isoformat()
        return None
    return None


# ── Verify, then choose ──────────────────────────────────────────────────────

def _cross_check(cid: str, token: str, quotes: "dict[str, dict]",
                 sids: "list[str]", pacer: "_Pacer", want: date) -> None:
    """
    Sanity-check the quote's close against daily history on a small sample.

    WARNING ONLY.  The source was already decided by the clock, and this must
    not overturn it — the history API runs a session behind for hours after the
    close, and letting it win is precisely the bug this whole path was rewritten
    for.  A sample that has not reached `want` is skipped, not counted against.
    """
    pool = [s for s in sids if quotes.get(s, {}).get("close")]
    if not pool:
        return
    sample = random.sample(pool, min(config.PREVCLOSE_SAMPLE, len(pool)))
    hits = checked = stale = 0
    bad = []
    for sid in sample:
        got = daily_prev_close(cid, token, sid, pacer)
        if got is None:
            continue
        hist, day = got
        if date.fromisoformat(day) != want:
            stale += 1
            continue
        q = quotes[sid]["close"]
        checked += 1
        if abs(q - hist) / hist * 100 <= MATCH_TOL_PCT:
            hits += 1
        else:
            bad.append((sid, q, hist))

    if checked == 0:
        print(f"  🔎 [{_now()}] cross-check: daily history abhi {want} pe nahi "
              f"pahunchi ({stale} sample purane din pe) — normal hai, quote "
              f"hi sahi hai")
        return
    if hits == checked:
        print(f"  🔎 [{_now()}] cross-check: {hits}/{checked} sample daily "
              f"history se match ✓")
        return
    print(f"  ⚠  [{_now()}] cross-check: sirf {hits}/{checked} match — "
          f"quote aur history alag keh rahe hain, dekh lo:")
    for sid, q, h in bad[:4]:
        print(f"       {sid:<8} quote {q:<10} history {h}")


def _f(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _cache_path(day: date) -> str:
    return config.PREVCLOSE_CACHE.format(day=day.isoformat())


def load(stocks: "list[dict]", cid: str, token: str,
         day: "date | None" = None,
         use_cache: bool = True) -> "dict[str, dict]":
    """
    sid -> {"prev_close", "prev_day", "ltp", "open", "high", "low", "volume",
            "source"}

    `ltp` and friends are the opening snapshot, so the page has something to
    draw before the first tick — and everything to draw when the market is shut.
    Only `prev_close` is cached; the snapshot is always refetched, because a
    cached LTP is a stale price wearing a live page's clothes.
    """
    import nse_holidays
    day = day or nse_holidays.session_day()
    want = expected_prev_day(day)
    sids = [s["security_id"] for s in stocks]
    path = _cache_path(day)
    print(f"  📅 [{_now()}] prev close chahiye {want} ka "
          f"(aaj {day}, {day.strftime('%a')})")

    cached: "dict[str, dict]" = {}
    if use_cache and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
            #  The cache records WHICH session it holds.  A file written before
            #  the history API caught up can contain the wrong session for the
            #  right date — that is exactly how a whole morning ran off Friday's
            #  close — so the day is checked, not just the filename.
            #  The SOURCE is part of what makes a cache valid, not just the
            #  session: switching db <-> quote changes every number, and a file
            #  written under the other setting would be reused silently.
            if (blob.get("_want") == want.isoformat()
                    and blob.get("_source") == _source()):
                cached = blob.get("rows") or {}
                have = sum(1 for s in sids if s in cached)
                print(f"  💾 [{_now()}] prev close cache mila — "
                      f"{have}/{len(sids)} stocks ({want} ka)")
            else:
                print(f"  🗑  [{_now()}] cache {blob.get('_want')} / "
                      f"{blob.get('_source')} ka hai, chahiye {want} / "
                      f"{_source()} — phenk kar dobara nikaal raha hoon")
        except Exception:
            cached = {}

    print(f"  📊 [{_now()}] Snapshot le raha hoon ({len(sids)} stocks, "
          f"1 request)...")
    quotes = bulk_quote(cid, token, sids)
    print(f"  📊 [{_now()}] {len(quotes)}/{len(sids)} stocks ka snapshot mila")

    out: "dict[str, dict]" = {}
    for s in stocks:
        sid = s["security_id"]
        q = quotes.get(sid, {})
        out[sid] = {"prev_close": None, "prev_day": None, "source": None,
                    "ltp": q.get("ltp"), "open": q.get("open"),
                    "high": q.get("high"), "low": q.get("low"),
                    "volume": q.get("volume") or 0}

    missing = [s for s in sids if not (cached.get(s) or {}).get("prev_close")]
    for sid in sids:
        c = cached.get(sid) or {}
        if c.get("prev_close"):
            out[sid]["prev_close"] = c["prev_close"]
            out[sid]["prev_day"] = c.get("prev_day")
            out[sid]["source"] = "cache"

    if not missing:
        print(f"  ✅ [{_now()}] prev close poora cache se aaya")
        return out

    #  ── THE decision, from the clock and the calendar ───────────────────────
    #  Source 1 when asked for: the 5-minute DB.  It has the right SESSION by
    #  construction and needs no API — but its last bar is 15:10, not the 15:30
    #  close, so this is a choice about WHICH number is wanted, not about which
    #  is cheaper.
    if _source() == "db":
        got = db_prev_close(missing, want)
        for sid, cl in got.items():
            out[sid].update(prev_close=cl, prev_day=want.isoformat(),
                            source="5min-db")
        print(f"  🗄  [{_now()}] {len(got)}/{len(missing)} prev close 5-min DB "
              f"se ({want} ka aakhri bar — 15:10, official close nahi)")
        missing = [s for s in missing if not out[s]["prev_close"]]
        if not missing:
            _save_cache(path, out, want)
            return out
        print(f"  ⚠  [{_now()}] {len(missing)} stocks DB me nahi mile — "
              f"unke liye API se le raha hoon")

    #  Not from sampling.  See quote_gives_prev_session().
    use_quote, why = quote_gives_prev_session(day)
    print(f"  🕐 [{_now()}] {why}")
    pacer = _Pacer()

    if use_quote:
        n = 0
        for sid in missing:
            c = quotes.get(sid, {}).get("close")
            if c:
                out[sid]["prev_close"] = c
                out[sid]["prev_day"] = want.isoformat()
                out[sid]["source"] = "quote"
                n += 1
        print(f"  ✅ [{_now()}] {n}/{len(missing)} prev close QUOTE se "
              f"(1 request, {want} ka close)")
        #  A sample is still checked against daily history — but only to SHOUT
        #  if the two disagree, never to override the rule above.  History that
        #  has not caught up is expected here and says nothing.
        _cross_check(cid, token, quotes, missing, pacer, want)
    else:
        print(f"  🐢 [{_now()}] Daily history se {len(missing)} stocks ka prev "
              f"close la raha hoon — thoda time lagega (rate limit)...")
        _fill_from_history(cid, token, missing, out, pacer, want)

    #  Still missing?  The 5-min DB has the right SESSION even when both APIs
    #  are unhelpful.  The quote's close is NOT used here — after the settle it
    #  is today's close, and a confidently wrong base is worse than a grey tile.
    gaps = [s for s in sids if not out[s]["prev_close"]]
    if gaps:
        got = db_prev_close(gaps, want)
        for sid, c in got.items():
            out[sid].update(prev_close=c, prev_day=want.isoformat(),
                            source="5min-db")
        if got:
            print(f"  🗄  [{_now()}] {len(got)}/{len(gaps)} prev close 5-min DB "
                  f"se (us din ka aakhri bar — 15:10, official close nahi)")
    holes = sum(1 for s in sids if not out[s]["prev_close"])
    if holes:
        print(f"  ⚠  [{_now()}] {holes} stocks ka prev close kahin se nahi "
              f"mila — wo grey rahenge")

    _save_cache(path, out, want)
    return out


def _fill_from_history(cid: str, token: str, sids: "list[str]",
                       out: "dict[str, dict]", pacer: "_Pacer",
                       want: date) -> None:
    """
    Daily history for each stock — but only a bar from `want` is accepted.

    A stock whose history has not reached that session is left for the caller's
    quote fallback.  Taking "whatever the newest bar is" is what produced a map
    priced off a session two back.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.time()
    done = ok = stale = 0
    with ThreadPoolExecutor(max_workers=HIST_WORKERS) as ex:
        futs = {ex.submit(daily_prev_close, cid, token, s, pacer): s
                for s in sids}
        for fut in as_completed(futs):
            sid = futs[fut]
            done += 1
            try:
                res = fut.result()
            except Exception:
                res = None
            if res and date.fromisoformat(res[1]) == want:
                out[sid].update(prev_close=res[0], prev_day=res[1],
                                source="history")
                ok += 1
            elif res:
                stale += 1
            if done % 25 == 0 or done == len(sids):
                print(f"       [{round(time.time() - t0)}s] {done}/{len(sids)}"
                      f" — {ok} mile · gap {pacer.gap:.2f}s "
                      f"· {pacer.throttled} throttles")
    print(f"  ✅ [{_now()}] {ok}/{len(sids)} prev close daily history se "
          f"({round(time.time() - t0)}s)")


def _save_cache(path: str, out: "dict[str, dict]", want: date) -> None:
    rows = {sid: {"prev_close": v["prev_close"], "prev_day": v["prev_day"],
                  "source": v["source"]}
            for sid, v in out.items() if v.get("prev_close")}
    #  `_want` is the whole point of the file: it records WHICH session these
    #  closes are, so a cache written while the history API was a day behind
    #  cannot be reused as if it were right.
    blob = {"_want": want.isoformat(), "_source": _source(), "rows": rows}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        print(f"  💾 [{_now()}] {len(rows)} prev close cache me save "
              f"({want} ka)")
    except Exception as e:
        print(f"  ⚠  cache save nahi hua: {e}")
    #  Yesterday's cache is dead weight the moment today's exists.
    folder = os.path.dirname(path)
    keep = os.path.basename(path)
    for name in os.listdir(folder):
        if name.startswith("_prevclose_") and name.endswith(".json") \
                and name != keep:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import universe
    cred = dhan_feed.ensure_feed()
    if not cred:
        raise SystemExit(1)
    stocks = universe.load()
    data = load(stocks, *cred, use_cache="--fresh" not in sys.argv)
    by_src: dict = {}
    for v in data.values():
        by_src[v["source"]] = by_src.get(v["source"], 0) + 1
    print(f"\n  sources: {by_src}\n")
    for s in stocks[:12]:
        v = data[s["security_id"]]
        pc, ltp = v["prev_close"], v["ltp"]
        pct = (ltp - pc) / pc * 100 if pc and ltp else None
        print(f"  {s['symbol']:<14} prev {pc}  ltp {ltp}  "
              f"{'' if pct is None else format(pct, '+.2f') + '%'}")
