# ─────────────────────────────────────────────────────────────────────────────
#  heatmap_ws.py  —  one Dhan socket, ~210 live prices, nothing else
#
#  The engine holds a single dict of the latest price per stock and hands out
#  snapshots.  It computes no indicators and keeps no history, so the tick path
#  is: parse 50 bytes, write four floats, mark the stock dirty.  That is all
#  that happens on the socket thread.
#
#  ── Why the socket thread does nothing else ─────────────────────────────────
#  The Dhan feed is a single read loop.  Anything slow inside on_message (an
#  HTTP post, a disk write, a long print) stalls that read and Dhan drops the
#  connection.  So the tick handler only mutates memory; a separate publisher
#  thread in main.py drains the dirty set on a timer and pushes to the browser.
#  One socket, one publisher thread, and nothing slow inside on_message.
#
#  ── QUOTE, not TICKER ───────────────────────────────────────────────────────
#  Subscribing with RequestCode 17 costs 50 bytes a packet instead of 16, and
#  buys the day's open/high/low and cumulative volume alongside the price.  The
#  volume is what lets the treemap size tiles by turnover; the day's range is
#  what the tooltip shows.  At 210 stocks the extra bandwidth is nothing.
#
#  ── Two failure modes that cost real sessions, and what is done about them ──
#  1. An account without the Data API add-on accepts the socket and then closes
#     it with no error frame — indistinguishable from a network blip.  So
#     dhan_feed.ensure_feed() asks REST first and this never connects blind.
#  2. A fixed retry delay against a permanent failure becomes a request every
#     3 seconds until Dhan 429s the whole IP.  Hence the exponential backoff,
#     reset only by a connection that actually survived a while.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import dhan_feed

# ── Dhan WS protocol ─────────────────────────────────────────────────────────
REQUEST_QUOTE = 17
RESP_TICKER = 2
RESP_QUOTE = 4
RESP_PREV_CLOSE = 6
RESP_DISCONNECT = 50
NSE_EQ_CODE = "NSE_EQ"

#  Quote packet body, after the 8-byte header:
#      ltp f · ltq h · ltt I · atp f · volume I · total sell I · total buy I
#      day open f · day close f · day high f · day low f          = 42 bytes
_QUOTE_BODY = struct.Struct("<fhIfIIIffff")
_HEADER = struct.Struct("<I")          # security id, at offset 4

CHUNK_SIZE = 100            # max instruments per subscribe message (Dhan docs)
KEEPALIVE_INTERVAL = 25     # Dhan closes silent connections after ~60s
RECONNECT_DELAY = 3         # first retry; doubles while connections die fast
MAX_RECONNECT_DELAY = 60    # Dhan 429s the IP if you hammer it
STATUS_INTERVAL = 30


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class HeatmapFeed:
    """
        feed = HeatmapFeed(stocks, seed)
        feed.run()                     # blocks until SCAN_END / stop()
        rows = feed.drain()            # whatever changed since the last call
    """

    def __init__(self, stocks: "list[dict]", seed: "dict[str, dict]",
                 cred: "tuple[str, str] | None" = None):
        self.stocks = stocks
        #  main.py already ran ensure_feed() to fetch the prev closes, so the
        #  credentials are handed down rather than re-derived.  Asking again
        #  would repeat the preflight and, worse, risk a second login on an
        #  account whose token the first one just made current.
        self.cred = cred
        self._by_id = {s["security_id"]: s for s in stocks}

        #  sid -> the numbers the page draws.  Seeded from the REST snapshot so
        #  the map is fully painted before the first tick — and stays painted on
        #  a weekend, when no tick will ever come.
        self.live: "dict[str, dict]" = {}
        for s in stocks:
            sid = s["security_id"]
            v = seed.get(sid, {})
            self.live[sid] = {
                "prev_close": v.get("prev_close"),
                "ltp": v.get("ltp"),
                "open": v.get("open"),
                "high": v.get("high"),
                "low": v.get("low"),
                "volume": v.get("volume") or 0,
                "ts": None,
            }

        self._dirty: "set[str]" = set(self.live)
        self._lock = threading.Lock()
        self._ws = None
        self._stop = threading.Event()

        self.ticks = 0
        self.seen: "set[str]" = set()
        self._last_status = time.time()
        #  Set when the preflight says this account cannot receive data.  The
        #  caller must STOP rather than retry — chasing past this point is what
        #  burned a whole session on 2026-08-10.
        self.feed_failed = False

    def snapshot(self) -> "list[dict]":
        """Every stock, whether or not it has ticked."""
        with self._lock:
            return [self._row(sid) for sid in self.live]

    def drain(self) -> "list[dict]":
        """Only what changed since the last call, and clear the mark."""
        with self._lock:
            sids, self._dirty = self._dirty, set()
        return [self._row(s) for s in sids]

    def _row(self, sid: str) -> dict:
        v = self.live[sid]
        s = self._by_id.get(sid, {})
        pc, ltp = v["prev_close"], v["ltp"]
        pct = ((ltp - pc) / pc * 100) if (pc and ltp) else None
        return {
            "sid": sid,
            "symbol": s.get("symbol", sid),
            "sector": s.get("sector", ""),
            "ltp": None if ltp is None else round(ltp, 2),
            "prev_close": None if pc is None else round(pc, 2),
            "chg": None if pct is None else round(ltp - pc, 2),
            "pct": None if pct is None else round(pct, 2),
            "open": None if v["open"] is None else round(v["open"], 2),
            "high": None if v["high"] is None else round(v["high"], 2),
            "low": None if v["low"] is None else round(v["low"], 2),
            #  Turnover, not share count — a 20 rupee stock and a 4000 rupee one
            #  are not comparable by volume, and turnover is what a treemap tile
            #  is meant to be proportional to.
            "turnover": round((v["volume"] or 0) * (ltp or 0)),
            "ts": v["ts"],
        }

    # ── The feed ─────────────────────────────────────────────────────────────

    def wait_for_open(self, poll: float = 5.0) -> None:
        """
        Sit until 09:15 if the market has not opened yet.

        Everything slow — the universe, the previous closes, the gate opens —
        is deliberately done BEFORE this, because none of it needs live data.
        Start at 09:05 and the map is fully built and already being served by
        the time the bell rings; the socket then opens with nothing left to do
        but receive.  Connecting earlier would only hold an idle socket that
        Dhan may drop before the session starts.
        """
        n = datetime.now()
        if n.weekday() >= 5:
            return
        mins = n.hour * 60 + n.minute
        if mins >= config.MARKET_OPEN_MIN:
            return
        target = n.replace(hour=9, minute=15, second=0, microsecond=0)
        wait = (target - n).total_seconds()
        print(f"  ⏳ [{_now()}] Market 09:15 pe khulega — "
              f"{int(wait // 60)}m {int(wait % 60)}s wait "
              f"(the map is already built, and the page stays up).")
        while not self._stop.is_set():
            n = datetime.now()
            if (n.hour * 60 + n.minute) >= config.MARKET_OPEN_MIN:
                break
            left = (target - n).total_seconds()
            if left > 60 and int(left) % 300 < poll:
                print(f"     ... {int(left // 60)}m baaki")
            time.sleep(min(poll, max(0.5, left)))
        if not self._stop.is_set():
            print(f"  🔔 [{_now()}] Market has opened — connecting.")

        #  Started after the close (or on a weekend): the seeded snapshot is
        #  already the day's final picture, so opening a socket would connect,
        #  be stopped by the watchdog seconds later, and change nothing.
        if self._past_end():
            print(f"  ⏸  [{_now()}] Market is closed — not opening the socket, "
                  f"the page shows the last prices of the session.")
            self._stop.set()
            return
        cred = self.cred or dhan_feed.ensure_feed()
        if cred is None:
            self.feed_failed = True
            return
        cid, token = cred

        self.wait_for_open()
        if self._stop.is_set():
            return

        threading.Thread(target=self._close_watchdog, daemon=True).start()
        url = dhan_feed.feed_url(cid, token)

        import websocket
        delay = RECONNECT_DELAY
        while not self._stop.is_set():
            connected_at = time.time()
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: print(f"  ⚠  [{_now()}] WS error: {e}"),
                    on_close=lambda ws, c, m: print(f"  🔌 [{_now()}] WS closed"))
                threading.Thread(target=self._keepalive, args=(self._ws,),
                                 daemon=True).start()
                #  ping_interval=0, NOT None: Dhan never answers a standard WS
                #  ping, so websocket-client's pong timeout would drop a
                #  perfectly healthy connection.
                self._ws.run_forever(ping_interval=0)
            except Exception as e:
                print(f"  ⚠  [{_now()}] WS crashed: {e}")
            if self._stop.is_set() or self._past_end():
                break

            if time.time() - connected_at > 60:
                delay = RECONNECT_DELAY      # it lived a while — a real blip
            else:
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
            print(f"  🔄 [{_now()}] reconnecting in {delay}s...")
            slept = 0.0
            while slept < delay and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _past_end(self) -> bool:
        n = datetime.now()
        return (n.hour, n.minute) >= config.SCAN_END

    def _close_watchdog(self):
        """
        End the session at SCAN_END.

        Checking `_past_end()` only between reconnects is not enough: a socket
        that simply STAYS UP runs past the close.  Dhan held one until 16:30 on
        2026-08-10 and the feed sat there for an hour after the bell.
        """
        while not self._stop.is_set():
            if self._past_end():
                print(f"  🔔 [{_now()}] {config.SCAN_END[0]:02d}:"
                      f"{config.SCAN_END[1]:02d} — market closed, stopping the feed.")
                self.stop()
                return
            time.sleep(10)

    def _keepalive(self, ws):
        while not self._stop.is_set():
            time.sleep(KEEPALIVE_INTERVAL)
            try:
                ws.send(json.dumps({"RequestCode": 11}))
            except Exception:
                return

    def _on_open(self, ws):
        sids = sorted({s["security_id"] for s in self.stocks})
        print(f"  🔗 [{_now()}] connected — subscribing {len(sids)} stocks")
        for i in range(0, len(sids), CHUNK_SIZE):
            chunk = sids[i:i + CHUNK_SIZE]
            ws.send(json.dumps({
                "RequestCode": REQUEST_QUOTE,
                "InstrumentCount": len(chunk),
                "InstrumentList": [{"ExchangeSegment": NSE_EQ_CODE,
                                    "SecurityId": s} for s in chunk]}))
            time.sleep(0.05)
        print(f"  👀 [{_now()}] live — the heatmap is updating\n")

    def _on_message(self, ws, message):
        if isinstance(message, str):
            return
        try:
            code = message[0]
        except Exception:
            return
        if code == RESP_DISCONNECT:
            print(f"  🔌 [{_now()}] server sent disconnect")
            return
        #  Nothing after the close counts.  Dhan kept the socket open until
        #  16:30 on 2026-08-10, and the trickle of post-close prints would move
        #  tiles that no chart would ever show moving.
        if self._past_end():
            return
        try:
            if code == RESP_QUOTE and len(message) >= 50:
                sid = str(_HEADER.unpack_from(message, 4)[0])
                (ltp, _ltq, _ltt, _atp, vol, _tsq, _tbq,
                 o, _c, h, lo) = _QUOTE_BODY.unpack_from(message, 8)
                self._apply(sid, ltp, vol, o, h, lo)
            elif code == RESP_TICKER and len(message) >= 12:
                sid = str(_HEADER.unpack_from(message, 4)[0])
                ltp = struct.unpack_from("<f", message, 8)[0]
                self._apply(sid, ltp)
        except Exception:
            return
        self._maybe_status()

    def _apply(self, sid: str, ltp: float, vol=None,
               o=None, h=None, lo=None) -> None:
        if ltp is None or ltp <= 0:
            return
        v = self.live.get(sid)
        if v is None:
            return                      # not in our universe — ignore
        self.ticks += 1
        self.seen.add(sid)
        v["ltp"] = float(ltp)
        v["ts"] = time.time()
        if vol:
            v["volume"] = int(vol)
        #  The day's O/H/L come from the exchange in the quote packet, so they
        #  are taken as given rather than tracked here — but a zero means "not
        #  sent yet", and overwriting a real open with 0 would break the tooltip.
        if o:
            v["open"] = float(o)
        if h:
            v["high"] = float(h)
        if lo:
            v["low"] = float(lo)

        with self._lock:
            self._dirty.add(sid)

    def _maybe_status(self):
        now = time.time()
        if now - self._last_status < STATUS_INTERVAL:
            return
        self._last_status = now
        print(f"  📡 [{_now()}] {self.ticks} ticks · {len(self.seen)} stocks live")
