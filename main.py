# ─────────────────────────────────────────────────────────────────────────────
#  main.py  —  NSE F&O live heatmap
#
#      python main.py                  sab kuch, ek command
#      python main.py --no-browser
#      python main.py --no-update      5-min DB refresh skip karo
#      python main.py --no-tunnel      public ngrok link mat banao
#      python main.py --snapshot       one REST snapshot, serve it, no socket
#      python main.py --fresh          ignore today's prev-close cache
#      python main.py --port 7001
#
#  ── The order of operations, and why it is this order ───────────────────────
#  1. universe    scrip master (once a day, validated), F&O list, what changed
#  2. DB          is candles_5min.db at the last completed session?  If not,
#                 fetch it.  FIRST, because everything below is computed off
#                 it — and because the updater logs in, and a new Dhan token
#                 kills the old one, so taking ours before it would be futile.
#  3. token       borrowed or fresh, then the Data-API preflight
#  4. prev close  resolved and VERIFIED — the denominator of every colour
#  5. serve       the page comes up already painted
#  6. wait        if it is before 09:15, sit until the bell — everything above
#                 needs no live data, so the map is ready when it rings
#  7. live        socket -> ticks -> page, every second
#
#  The publisher between (6) and (7) is what keeps the socket thread clean: the
#  feed marks stocks dirty, this drains them on a timer and pushes.  Nothing
#  slow ever runs inside on_message.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config
import db_update
import dhan_feed
import frontend_heatmap as fe
import heatmap_ws
import nse_holidays
import prevclose
import sectors
import universe


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _arg(name: str, default=None):
    argv = sys.argv[1:]
    if name in argv:
        try:
            return argv[argv.index(name) + 1]
        except IndexError:
            return default
    return default


def publisher(feed: "heatmap_ws.HeatmapFeed", stop: threading.Event) -> None:
    """
    Drains whatever changed and pushes it to the browser, on a timer.

    This is what keeps the socket thread clean: ticks only mark a stock dirty
    and write numbers into memory, and every slow thing — building rows,
    encoding JSON, fanning out to subscribers — happens here instead.
    """
    push_every = float(config.PUSH_INTERVAL_SEC)
    while not stop.is_set():
        time.sleep(push_every)
        try:
            rows = feed.drain()
            if rows:
                fe.publish(rows)
        except Exception as e:
            print(f"  ⚠  publish fail: {e}")


def main() -> int:
    argv = sys.argv[1:]
    port = int(_arg("--port", config.PORT))
    #  "Aaj" ka matlab yahan JO SESSION DIKH RAHA HAI — Itwaar ko wo
    #  Friday hai.  Ye ek line prev_close aur cache ka naam, dono tay
    #  karti hai.
    today = nse_holidays.session_day()

    print("=" * 74)
    print("  NSE F&O  —  LIVE HEATMAP")
    print("=" * 74)

    # 1 ── universe (scrip master: once a day, validated)
    stocks = universe.load()
    if not stocks:
        print("  ❌ Universe khaali hai — scrip master check karo.")
        return 1
    for s in stocks:
        s["sector"] = sectors.of(s["symbol"])

    # 2 ── is the 5-min DB current?  Update it BEFORE taking a token.
    #
    #  The prev closes are resolved against this DB, and a DB one session
    #  behind produces a heatmap that looks perfectly normal while every colour
    #  is measured from the wrong day.  So the check comes first.
    #
    #  It runs FIRST for a second reason: the updater script logs in, and a new
    #  Dhan token invalidates the old one server-side.  Taking our token before
    #  this would mean holding one the updater then kills — so the order is
    #  update, THEN token.  (ensure_feed's preflight also catches a superseded
    #  token and re-logs-in, but not needing that is better.)
    db = db_update.ensure(allow_update="--no-update" not in argv)

    # 3 ── credentials, then prev close
    cred = dhan_feed.ensure_feed()
    if cred is None:
        return 1
    cid, token = cred

    seed = prevclose.load(stocks, cid, token, day=today,
                          use_cache="--fresh" not in argv)
    have_pc = sum(1 for v in seed.values() if v.get("prev_close"))
    have_px = sum(1 for v in seed.values() if v.get("ltp"))
    print(f"  🎨 [{_now()}] {have_pc}/{len(stocks)} prev close · "
          f"{have_px}/{len(stocks)} price — map paint hone ko taiyaar")

    # 4 ── serve, already painted
    feed = heatmap_ws.HeatmapFeed(stocks, seed, cred=cred)
    fe.publish(feed.snapshot())
    fe.start_server(port)
    fe.set_status(f"Snapshot — {len(stocks)} stocks", live=False)

    print(f"\n  🖥️  Heatmap        →  http://127.0.0.1:{port}/")
    print(f"  🌐 Same WiFi se   →  http://{fe.lan_ip()}:{port}/")
    if getattr(config, "NGROK_ENABLED", False) and "--no-tunnel" not in argv:
        import tunnel
        url = tunnel.start(port)
        if url:
            print(f"  🔗 Public link    →  {url}")
            print(f"     (kisi bhi network se chalega — PUBLIC hai, "
                  f"share mat karna)")
    print(f"  💡 Kisi bhi tile pe click karo → 5-min TradingView chart\n")
    if "--no-browser" not in argv:
        fe.open_browser(port)

    if "--snapshot" in argv:
        print("  --snapshot: socket nahi khol raha.  Ctrl-C se band karo.\n")
        fe.set_status("Snapshot only (feed off)", live=False)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  🛑 Bye.\n")
        return 0

    # 5 ── the feed
    stop = threading.Event()
    threading.Thread(target=publisher, args=(feed, stop), daemon=True).start()
    fe.set_status(f"LIVE — {len(stocks)} stocks", live=True)

    try:
        feed.run()
    except KeyboardInterrupt:
        print("\n  🛑 Stopping...")
        feed.stop()
    finally:
        stop.set()

    #  Whatever arrived in the last window still deserves to reach the page.
    try:
        fe.publish(feed.drain())
    except Exception:
        pass

    if feed.feed_failed:
        fe.set_status("Feed unavailable — snapshot only", live=False)
        print("\n  ⚠  Feed nahi chala.  Page abhi bhi khula hai (snapshot ke "
              "saath).  Ctrl-C se band karo.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 1

    fe.set_status("Market band — aakhri prices", live=False)
    print(f"\n  Done — {feed.ticks} ticks, {len(feed.seen)} stocks.  "
          f"Page khula hai, Ctrl-C se band karo.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("  🛑 Bye.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
