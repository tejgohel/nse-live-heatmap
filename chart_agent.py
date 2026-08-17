# ─────────────────────────────────────────────────────────────────────────────
#  chart_agent.py  —  the small file that runs on the VIEWER's PC
#
#      python chart_agent.py
#
#  ── Why this is needed ─────────────────────────────────────────────────────
#  The heatmap runs on your PC; someone else watches it over the LAN link. When
#  they click a symbol, the chart has to open on THEIR second screen, in THEIR
#  Chrome profile.
#
#  The server simply cannot do that. chartwin.py launches Chrome on the SERVER's
#  desktop — their click would open a window on your screen, not theirs. And no
#  web page can touch another computer's desktop; that is one of the browser's
#  most basic security lines, and no trick gets around it.
#
#  So this one small job runs on their PC instead. Everything else — universe,
#  previous close, the feed — stays on yours. All this file does is: "open the
#  chart for this symbol".
#
#  ── What it needs ──────────────────────────────────────────────────────────
#  Python only (no pip install — pure stdlib), and TWO files:
#      chart_agent.py · chartwin.py
#
#  ⚠  Do not send config.py. auto_login writes the LIVE Dhan ACCESS_TOKEN into
#     it on every login, and it also holds this machine's paths. These two files
#     run fine without it — chartwin falls back to defaults when config is
#     missing. And never send auto_login.py at all: it holds the client id, the
#     PIN and the TOTP secret.
#
#  ── Safety ─────────────────────────────────────────────────────────────────
#  It binds to 127.0.0.1 only, so nothing on the network can reach it — just the
#  browser on that same PC. It opens TradingView chart URLs and nothing else:
#  the URL is built here from the symbol, never taken from the caller.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import chartwin

#  chartwin exposes whatever config it found — the real one here, defaults on a
#  machine that only has these two files.  config.py is NOT shipped with the
#  agent on purpose: auto_login writes the live Dhan ACCESS_TOKEN into it.
PORT = int(getattr(chartwin.config, "AGENT_PORT", 7011))

#  Symbols only — NSE tickers are letters, digits and a few separators.  The
#  URL is built here from the symbol; a caller-supplied URL is never opened.
_SYM_OK = re.compile(r"^[A-Za-z0-9&._-]{1,24}$")


class Handler(BaseHTTPRequestHandler):

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        #  The heatmap page is served from the OTHER machine, so every call
        #  here is cross-origin.  A plain GET with no custom header is a
        #  "simple request", so this one header is all CORS needs — no
        #  preflight to answer.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _here(self, q: dict) -> "tuple | None":
        try:
            return int(float(q["sx"][0])), int(float(q["sy"][0]))
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/ping":
            here = self._here(q)
            mons = chartwin.monitors()
            target = chartwin.chart_monitor(here)
            self._send({"ok": bool(target) and bool(chartwin._chrome()),
                        "agent": True, "count": len(mons), "monitors": mons,
                        "target": target, "chrome": bool(chartwin._chrome()),
                        "profile": chartwin.profile_args(here)})
            return

        if u.path == "/chart":
            sym = (q.get("symbol") or [""])[0].strip()
            if not _SYM_OK.match(sym):
                self._send({"ok": False, "msg": "invalid symbol"}, 400)
                return
            try:
                ok, msg = chartwin.open_chart(sym, here=self._here(q))
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            print(f"  {'✅' if ok else '❌'} {sym:<14} {msg}")
            self._send({"ok": ok, "msg": msg})
            return

        self._send({"ok": False, "msg": "not found"}, 404)

    def log_message(self, *a):        # keep the console to our own lines
        pass


def main() -> int:
    mons = chartwin.monitors()
    print("=" * 66)
    print("  CHART AGENT  —  heatmap ke chart is PC pe kholta hai")
    print("=" * 66)
    print(f"  Monitors ({len(mons)}):")
    for i, m in enumerate(mons):
        print(f"    [{i}] {m['width']}x{m['height']} @ {m['left']},{m['top']}"
              + ("  primary" if m["primary"] else ""))
    if len(mons) < 2:
        print("  ⚠  Ek hi monitor mila — chart normal tab me khulega.")
    ch = chartwin._chrome()
    print(f"  Chrome   : {ch or 'NOT FOUND — set CHROME_PATH in config.py'}")
    print(f"  Listening: http://127.0.0.1:{PORT}   (this PC only)")
    print("=" * 66)
    print("  Now open the heatmap and tick '⧉ Chart 2nd screen'.")
    print("  Ise chalta chhod do.  Ctrl-C se band.\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye.\n")
    except OSError as e:
        print(f"\n  ❌ Could not bind port {PORT}: {e}")
        print(f"     An agent is probably already running.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
