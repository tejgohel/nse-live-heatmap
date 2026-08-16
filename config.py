# ─────────────────────────────────────────────────────────────────────────────
#  config.py  —  every tunable in one place
#
#  ⚠️  NO SECRETS IN THIS FILE.
#      Credentials are read from environment variables (or a local .env, which
#      is git-ignored). Copy .env.example → .env and fill it in.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Windows consoles default to cp1252 and cannot encode the box-drawing and
# emoji characters used throughout the output. config is imported by every
# entry point, so this is the one place worth fixing it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ── Optional .env loader (no hard dependency on python-dotenv) ───────────────
def _load_dotenv() -> None:
    path = os.path.join(_HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


# ═════════════════════════════════════════════════════════════════════════════
#  👉  YOUR BROKER CREDENTIALS GO HERE — but NOT in this file.
#
#      Both values are read from environment variables so nothing secret is
#      ever committed. To connect your own Dhan account:
#
#        1. copy  .env.example  →  .env
#        2. fill in your own values:
#
#             DHAN_CLIENT_ID     your Dhan client ID    (e.g. 11XXXXXXXX)
#             DHAN_ACCESS_TOKEN  your API access token  (long JWT string)
#             DHAN_PIN           your Dhan login PIN    (optional, for TOTP)
#             DHAN_TOTP_SECRET   your base32 2FA seed   (optional, for TOTP)
#
#      Where to get them:
#        Client ID + Access Token → https://dhanhq.co → My Profile
#                                   → DhanHQ Trading APIs → Generate Token
#        TOTP secret              → the text behind the QR code when you enable
#                                   2FA on Dhan; with it, auto_login.py mints a
#                                   fresh token on every run
#
#      ⚠️  Do NOT paste real values below. `.env` is in .gitignore; config.py
#          is NOT — anything hardcoded here WILL be published when you push.
# ═════════════════════════════════════════════════════════════════════════════

CLIENT_ID    = os.getenv("DHAN_CLIENT_ID",    "")   # ← set in .env
ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")   # ← set in .env
DHAN_PIN         = os.getenv("DHAN_PIN",         "")   # ← set in .env
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "")   # ← set in .env


# ── Dashboard ────────────────────────────────────────────────────────────────
PORT = int(os.getenv("HEATMAP_PORT", "7000"))


# ── Remote access (another network) ──────────────────────────────────────────
#  Flask binds to 0.0.0.0, so any PC or phone on the SAME WiFi can open the LAN
#  IP printed at startup. To reach it from a different network you need a
#  tunnel; ngrok is wired up but off by default.
#
#  ⚠  A tunnel URL is PUBLIC — anyone with the link sees the heatmap. Only
#     prices, sector and day OHLC ever reach the page: no token, no
#     credentials, no orders. Still, do not share the link.
#
#  A free-tier URL changes on every restart. For a fixed one, take a static
#  domain from the ngrok dashboard and put it in NGROK_DOMAIN.
NGROK_ENABLED = os.getenv("NGROK_ENABLED", "0") == "1"
NGROK_PATH    = os.getenv("NGROK_PATH", "ngrok")   # on PATH, or an absolute path
NGROK_DOMAIN  = os.getenv("NGROK_DOMAIN", "")


# ── Chart window (second screen) ─────────────────────────────────────────────
#  Where the chart opens when you click a tile. chartwin.py asks Windows for
#  each monitor's real position, so there are no coordinates here.
#    CHART_MONITOR = -1   ->  whichever monitor is not primary (default)
#    CHART_MONITOR = 0/1  ->  the index `python chartwin.py` prints
#  Leave CHROME_PATH empty to auto-detect.
CHART_MONITOR = -1
CHROME_PATH   = ""

#  Which Chrome profile the chart window uses:
#    "same"     your normal profile — TradingView login and layouts intact
#    "separate" a blank profile (_chartprofile/), so you log in again
CHART_PROFILE = "same"

#  For someone viewing from another PC: they run `python chart_agent.py` there,
#  it listens on this port (127.0.0.1 only), and the page calls it so the chart
#  opens on THEIR second screen. A server cannot touch another machine's
#  desktop, which is why that small helper exists.
AGENT_PORT = 7011


# ── Push cadence ─────────────────────────────────────────────────────────────
#  How often changed rows are pushed to the browser. This is purely cosmetic,
#  so keeping it slow is cheap: relaying out ~200 tiles every second makes the
#  map twitch and wastes bandwidth. Ticks are still applied in memory the
#  instant they arrive.
PUSH_INTERVAL_SEC = float(os.getenv("PUSH_INTERVAL_SEC", "30"))


# ── Colour buckets ───────────────────────────────────────────────────────────
#  Top to bottom:
#      change% >= 2          -> green
#      0.50 .. 2             -> light green
#      0.01 .. 0.50          -> whitish green
#      |change%| < 0.01      -> flat / neutral
#      and the exact mirror on the down side.
#
#  DARK green/red is NOT another threshold — it is a RANK. On a strong day the
#  top gainer is +11%, on a quiet one +2.5%; a fixed cut-off would paint every
#  tile dark on the first and none on the second. So the TOP_N by change% get
#  the dark shade, and MIN_EXTREME_PCT stops a flat day from crowning a +0.3%
#  stock.
BAND_STRONG = 2.0
BAND_MILD   = 0.50
BAND_FLAT   = 0.01

TOP_N           = 5      #  how many names wear the dark shade at each end
MIN_EXTREME_PCT = 2.0    #  ...and only if they cleared the strong band


# ─────────────────────────────────────────────────────────────────────────────
#  Previous close  —  the denominator of every colour on the page
#
#  change% = (LTP - prev_close) / prev_close * 100, so this one setting decides
#  what the whole map looks like. prevclose.py resolves it and verifies it.
#
#    "quote"  the broker's OFFICIAL close (15:30). What every other site shows.
#             Needs nothing but the API — this is the default, and the only
#             mode a fresh clone needs.
#
#    "db"     the last 5-min bar (15:10) of that session, out of a local
#             candles_5min.db. The intraday feed stops at 15:10, so the last
#             ~20 minutes are missing and the % differs slightly from a
#             broker's "day change" — but it matches the last candle on a
#             5-minute chart exactly. Only pick this if you already maintain
#             such a database; see FIVEMIN_DB below.
# ─────────────────────────────────────────────────────────────────────────────
PREVCLOSE_SOURCE = os.getenv("PREVCLOSE_SOURCE", "quote")

PREVCLOSE_CACHE  = os.path.join(_HERE, "_prevclose_{day}.json")
PREVCLOSE_SAMPLE = 8     #  stocks used to verify the cheap bulk source


# ── Optional 5-minute candle database (PREVCLOSE_SOURCE = "db" only) ─────────
#  An external SQLite file holding 5-minute OHLCV, one table per security. It
#  is opened READ-ONLY. Leave FIVEMIN_DB pointing at a file that does not exist
#  and the "db" mode simply falls back to "quote".
#
#  FIVEMIN_UPDATER, if set, is a script run as a subprocess to refresh that
#  database before the map is built. Leave it empty to skip the step entirely.
FIVEMIN_DB      = os.getenv("FIVEMIN_DB", os.path.join(_HERE, "candles_5min.db"))
FIVEMIN_TABLE   = os.getenv("FIVEMIN_TABLE", "candles_5min_{sid}")
FIVEMIN_UPDATER = os.getenv("FIVEMIN_UPDATER", "")


# ── Instrument master ────────────────────────────────────────────────────────
#  Downloaded once a day and validated. Extra fallback copies can be listed
#  here (absolute paths) if you keep the same CSV elsewhere.
SCRIP_MASTER_FALLBACKS: list[str] = []


# ── Feed ─────────────────────────────────────────────────────────────────────
MARKET_OPEN_MIN = 9 * 60 + 15    # 09:15 IST, as minutes from midnight
SCAN_END        = (15, 40)       # stop the feed at 15:40 IST
