# ─────────────────────────────────────────────────────────────────────────────
#  universe.py  —  the stocks the heatmap draws
#
#  The heatmap needs only a live price and a previous close, so a stock is in
#  as soon as it has an equity security_id — no local history required.
#
#      F&O list  = distinct underlyings of NSE FUTSTK rows
#                  ("RELIANCE-Aug2026-FUT" -> "RELIANCE")
#      secid     = SEM_SMST_SECURITY_ID of the NSE EQUITY / series EQ row
#
#  Source = Dhan's scrip master CSV, downloaded once a day.  On failure it
#  falls back to any older copy listed in config.SCRIP_MASTER_FALLBACKS — same
#  schema, just older.  The count drifts as SEBI revises the F&O list monthly;
#  that is expected, the universe is rebuilt every run.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd

import config

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_MASTER_PATH = os.path.join(_HERE, "scrip_master.csv")

#  When the CSV on disk was actually downloaded, and what came out of it.
#  The file's mtime used to be the only record of that, and mtime lies in both
#  directions: copying the file in from another project stamps it with today
#  (so a stale list looks fresh), and any tool that rewrites it resets the
#  clock.  A sidecar written only on a VERIFIED download says what happened.
STAMP_PATH = os.path.join(_HERE, "_scripmaster.json")

#  Yesterday's resolved universe, so a run can say what SEBI changed.
LAST_UNIVERSE = os.path.join(_HERE, "_universe.json")

#  A good scrip master is ~25 MB with thousands of FUTSTK rows.  Anything far
#  under that is a truncated download or an error page, and must not be allowed
#  to replace a working file.
MIN_CSV_BYTES = 5_000_000
MIN_FUTSTK_ROWS = 50

#  Only reached when today's download fails: any older copy of the same CSV
#  will still resolve the F&O list, and a day-old master beats no map at all.
#  Add absolute paths via config.SCRIP_MASTER_FALLBACKS.  Read-only.
FALLBACK_PATHS = list(getattr(config, "SCRIP_MASTER_FALLBACKS", []))

_USECOLS = ["SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_SERIES", "SEM_SMST_SECURITY_ID"]


def _fut_underlying(trading_symbol: str) -> str:
    """
    Underlying symbol from a stock-future trading symbol — strip the trailing
    '-<expiry>-FUT'.  Splitting on '-' rather than regex-matching the expiry
    keeps hyphenated names intact:

        'RELIANCE-Aug2026-FUT'    -> 'RELIANCE'
        'BAJAJ-AUTO-Aug2026-FUT'  -> 'BAJAJ-AUTO'
        'M&M-Aug2026-FUT'         -> 'M&M'
    """
    parts = str(trading_symbol).split("-")
    return "-".join(parts[:-2]) if len(parts) >= 3 else parts[0]


def _validate(path: str) -> "int | None":
    """FUTSTK row count if `path` is a usable scrip master, else None."""
    try:
        if os.path.getsize(path) < MIN_CSV_BYTES:
            return None
        df = pd.read_csv(path, low_memory=False,
                         usecols=["SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME"])
    except Exception:
        return None
    n = int(((df["SEM_EXM_EXCH_ID"] == "NSE") &
             (df["SEM_INSTRUMENT_NAME"] == "FUTSTK")).sum())
    return n if n >= MIN_FUTSTK_ROWS else None


def _download(retries: int = 3) -> bool:
    """
    Fetch a fresh scrip master, and only replace the working one if the new
    file is actually good.

    Downloading straight over SCRIP_MASTER_PATH was the risk: a connection that
    dies halfway leaves a truncated CSV carrying TODAY's date, so every later
    run of the day accepts it as fresh and the universe silently shrinks.  So
    it lands in a .tmp, gets parsed and counted, and only then replaces the
    real file.  A bad download now costs nothing — yesterday's list survives.
    """
    import requests
    tmp = SCRIP_MASTER_PATH + ".tmp"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(SCRIP_MASTER_URL, timeout=120)
            r.raise_for_status()
            with open(tmp, "wb") as f:
                f.write(r.content)
            rows = _validate(tmp)
            if rows is None:
                print(f"  ⚠  Download aaya par CSV theek nahi lagi "
                      f"({os.path.getsize(tmp):,} bytes) — reject "
                      f"({attempt}/{retries})")
                os.remove(tmp)
                continue
            os.replace(tmp, SCRIP_MASTER_PATH)     # atomic on Windows and POSIX
            with open(STAMP_PATH, "w", encoding="utf-8") as f:
                json.dump({"date": date.today().isoformat(), "futstk": rows,
                           "bytes": os.path.getsize(SCRIP_MASTER_PATH)}, f)
            print(f"  ✅ Scrip master updated — {rows} FUTSTK rows")
            return True
        except Exception as e:
            print(f"  ⚠  Scrip master download fail "
                  f"({attempt}/{retries}): {type(e).__name__}")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return False


def _downloaded_on() -> "date | None":
    """The day the CSV on disk was verified-downloaded, or None."""
    if not os.path.exists(SCRIP_MASTER_PATH):
        return None
    try:
        with open(STAMP_PATH, encoding="utf-8") as f:
            return date.fromisoformat(json.load(f)["date"])
    except Exception:
        #  No stamp yet (first run after this file gained one).  Fall back to
        #  mtime rather than forcing a needless 25 MB download.
        try:
            return date.fromtimestamp(os.path.getmtime(SCRIP_MASTER_PATH))
        except OSError:
            return None


def _scrip_master(force_refresh: bool = False) -> str:
    """
    Path to a scrip master, downloaded at most ONCE PER DAY.

    SEBI revises the F&O list monthly, so the first run each morning refetches
    it and every later run that day reuses the file — the check is the stamp
    above, not the clock, so restarting the heatmap ten times costs one
    download.
    """
    got_on = _downloaded_on()
    if got_on == date.today() and not force_refresh:
        print(f"  📄 Scrip master aaj hi download ho chuki hai — "
              f"dobara nahi kar raha")
        return SCRIP_MASTER_PATH

    if got_on:
        print(f"  📥 Scrip master {got_on} ki hai — naya download kar raha "
              f"hoon (F&O list badalti rehti hai)...")
    else:
        print("  📥 Dhan scrip master download kar raha hoon (F&O list)...")
    if _download():
        return SCRIP_MASTER_PATH
    if os.path.exists(SCRIP_MASTER_PATH):
        print(f"  ↩  Download nahi hua — {got_on} wali scrip_master.csv use "
              f"kar raha hoon.")
        return SCRIP_MASTER_PATH
    for p in FALLBACK_PATHS:
        if os.path.exists(p):
            print(f"  ↩  Fallback: {p}")
            return p
    raise FileNotFoundError("koi scrip master nahi mila")


def load(force_refresh: bool = False, quiet: bool = False) -> "list[dict]":
    """[{"symbol", "security_id"}, ...] for every NSE stock-futures underlying."""
    path = _scrip_master(force_refresh)
    df = pd.read_csv(path, low_memory=False, usecols=_USECOLS)
    nse = df[df["SEM_EXM_EXCH_ID"] == "NSE"]

    fut = nse[nse["SEM_INSTRUMENT_NAME"] == "FUTSTK"]["SEM_TRADING_SYMBOL"].astype(str)
    underlyings = {u for u in (_fut_underlying(s) for s in fut)
                   if u and "TEST" not in u.upper()}

    #  series EQ only — BE/BZ are trade-to-trade and price differently.
    eq = nse[(nse["SEM_INSTRUMENT_NAME"] == "EQUITY") & (nse["SEM_SERIES"] == "EQ")]
    sym_to_sid: "dict[str, str]" = {}
    for sym, sid in zip(eq["SEM_TRADING_SYMBOL"].astype(str),
                        eq["SEM_SMST_SECURITY_ID"]):
        sym_to_sid.setdefault(sym, str(int(sid)))

    resolved, unresolved = [], []
    for sym in sorted(underlyings):
        sid = sym_to_sid.get(sym)
        if sid is None:
            unresolved.append(sym)
            continue
        resolved.append({"symbol": sym, "security_id": sid})

    changes = _diff_and_remember(resolved, write=not quiet)

    if not quiet:
        print(f"\n{'═' * 64}")
        print(f"  F&O UNIVERSE  —  {len(resolved)} stocks")
        print(f"  Source : {os.path.basename(path)}")
        print(f"  FUTSTK underlyings : {len(underlyings)}")
        if unresolved:
            print(f"  ⚠  No EQ scrip ({len(unresolved)}): "
                  f"{', '.join(unresolved[:10])}"
                  f"{' …' if len(unresolved) > 10 else ''}")
        if changes["added"]:
            print(f"  ➕ NAYE stocks ({len(changes['added'])}): "
                  f"{', '.join(changes['added'])}")
        if changes["removed"]:
            print(f"  ➖ NIKAL gaye ({len(changes['removed'])}): "
                  f"{', '.join(changes['removed'])}")
        if changes["first_run"]:
            print(f"  (pehla run — agli baar se badlaav dikhenge)")
        elif not changes["added"] and not changes["removed"]:
            print(f"  ✓ pichhle run se koi badlaav nahi")
        print(f"{'═' * 64}\n")
    return resolved


def _diff_and_remember(stocks: "list[dict]", write: bool = True) -> dict:
    """
    What SEBI changed since the last run, and remember today's list.

    The universe is rebuilt from scratch every run, so a stock that joins the
    F&O list is picked up automatically — but silently, and a name appearing or
    vanishing from the map is exactly the thing worth being told about.  A stock
    A stock that JOINS has no previous close cached yet either, so it paints
    grey until the next resolve.
    """
    now = {s["symbol"] for s in stocks}
    prev: "set[str]" = set()
    first = True
    try:
        with open(LAST_UNIVERSE, encoding="utf-8") as f:
            blob = json.load(f)
        prev = set(blob.get("symbols") or [])
        first = not prev
    except Exception:
        pass

    out = {"added": sorted(now - prev) if not first else [],
           "removed": sorted(prev - now) if not first else [],
           "first_run": first}
    if write:
        try:
            with open(LAST_UNIVERSE, "w", encoding="utf-8") as f:
                json.dump({"date": date.today().isoformat(),
                           "symbols": sorted(now)}, f)
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    stocks = load()
    for s in stocks[:15]:
        print(f"  {s['symbol']:<16} -> {s['security_id']}")
    print(f"  ... total {len(stocks)}")
