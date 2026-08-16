# ─────────────────────────────────────────────────────────────────────────────
#  chart_agent.py  —  DOST ke PC pe chalane wali chhoti file
#
#      python chart_agent.py
#
#  ── Ye kyun chahiye ────────────────────────────────────────────────────────
#  Heatmap tumhare PC pe chalta hai; dost use LAN link se dekhta hai.  Jab wo
#  kisi symbol pe click kare to chart USKI doosri screen pe, USKI Chrome
#  profile me khulna chahiye.
#
#  Wo server se ho hi nahi sakta.  chartwin.py Chrome ko SERVER ke desktop pe
#  chalata hai — dost ke click pe window tumhari screen pe khulti, uski nahi.
#  Aur koi bhi web page kisi doosre computer ka desktop chhoo nahi sakta; ye
#  browser ki sabse buniyadi security line hai, koi trick isse nahi todti.
#
#  Isliye ye ek line ka kaam dost ke PC pe hota hai.  Baaki sab — universe,
#  prev close, feed — tumhare PC pe hi rehta hai.  Ye file sirf itna karti
#  hai: "is symbol ka chart kholo".
#
#  ── Zaroorat ───────────────────────────────────────────────────────────────
#  Sirf Python (koi pip install nahi — poori stdlib), aur DO file:
#      chart_agent.py · chartwin.py
#
#  ⚠  config.py mat bhejna.  auto_login har login pe usme LIVE Dhan
#     ACCESS_TOKEN likh deta hai, aur usme is machine ke paths bhi hain.
#     Ye dono file usse bina chal jaati hain — chartwin config na mile to
#     defaults use karta hai.  auto_login.py to bilkul mat bhejna: usme
#     client id, PIN aur TOTP secret hai.
#
#  ── Suraksha ───────────────────────────────────────────────────────────────
#  127.0.0.1 pe hi bind hota hai, to network se koi ise chhoo nahi sakta —
#  sirf usi PC ka browser.  Ye sirf TradingView ke chart URL kholta hai; symbol
#  se URL yahin banta hai, bhejne wale ka diya URL kabhi nahi kholta.
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
                self._send({"ok": False, "msg": "symbol theek nahi"}, 400)
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
    print(f"  Chrome   : {ch or 'NAHI MILA — config.py me CHROME_PATH set karo'}")
    print(f"  Sun raha : http://127.0.0.1:{PORT}   (sirf isi PC se)")
    print("=" * 66)
    print("  Ab heatmap kholo aur '⧉ Chart 2nd screen' tick karo.")
    print("  Ise chalta chhod do.  Ctrl-C se band.\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye.\n")
    except OSError as e:
        print(f"\n  ❌ Port {PORT} pe nahi baith paaya: {e}")
        print(f"     Shayad agent pehle se chal raha hai.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
