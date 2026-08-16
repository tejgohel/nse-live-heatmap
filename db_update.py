# ─────────────────────────────────────────────────────────────────────────────
#  db_update.py  —  "kya 5-min DB latest hai?" aur nahi hai to update
#
#      python db_update.py            # check + update if stale
#      python db_update.py --check    # sirf batao, kuch chalao mat
#
#  ── Why this exists ─────────────────────────────────────────────────────────
#  Only used when PREVCLOSE_SOURCE = "db": the previous close is then read from
#  a local 5-minute candle database.  A DB one session behind does not fail —
#  it produces a heatmap where every colour is measured from the wrong day,
#  which looks completely normal and is completely wrong.  So the check happens
#  before anything is painted.
#
#  ── What "up to date" means ─────────────────────────────────────────────────
#  The DB should reach the last COMPLETED trading session:
#
#      running after ~15:40 on a trading day   ->  today
#      any other time                          ->  the previous trading day
#
#  Weekends and NSE holidays come from nse_holidays.py, so a Monday morning run
#  expects Friday and does not try to fetch a session that never happened.
#
#  ── The update is a SUBPROCESS ──────────────────────────────────────────────
#  config.FIVEMIN_UPDATER points at whatever script maintains that database.
#  It is run as a subprocess rather than imported, because such a script tends
#  to do module-level work against its own config.  If it logs in it will mint
#  a NEW Dhan token, which invalidates the old one server-side — which is why
#  main.py runs this BEFORE taking a token for the live feed, and never while
#  a feed is connected.  Leave FIVEMIN_UPDATER empty to skip the step.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config
import nse_holidays

UPDATE_SCRIPT = getattr(config, "FIVEMIN_UPDATER", "") or ""

#  A session is only counted as "completed" once the feed has stopped writing.
#  15:40 rather than 15:30: the DB's last bar is 15:10, and update_db_5min needs
#  the exchange to have settled the tail of the session.
SESSION_DONE_MIN = 15 * 60 + 40

#  ── Dhan does not publish today's 5-minute bars the instant the bell rings ──
#  Observed 2026-08-10: the session ended 15:30, and candles_5min.db only gained
#  today's 72 bars at 16:40.  So between 15:40 and whenever Dhan is ready, an
#  update is expected to come back with nothing — that is normal, not a fault.
#
#  Without a memory of that, restarting main.py five times in that window would
#  burn five full update runs to fetch nothing.  So a fruitless attempt is
#  recorded and not repeated for RETRY_COOLDOWN_MIN minutes.
ATTEMPT_STAMP = os.path.join(_HERE, "_dbupdate_attempt.json")
RETRY_COOLDOWN_MIN = 20

#  Probed on one liquid stock rather than scanning 208 tables — RELIANCE has
#  never been missing from this DB, and MAX(date) on one table is instant.
PROBE_SID = "2885"


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def db_day() -> "date | None":
    """The newest day candles_5min.db holds, or None if unreadable."""
    if not os.path.exists(config.FIVEMIN_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{config.FIVEMIN_DB}?mode=ro", uri=True,
                              timeout=60)
    except Exception:
        return None
    try:
        row = con.execute(
            f'SELECT MAX(date) FROM '
            f'"{config.FIVEMIN_TABLE.format(sid=PROBE_SID)}"').fetchone()
    except Exception:
        return None
    finally:
        con.close()
    return date.fromisoformat(row[0][:10]) if row and row[0] else None


def expected_day(now: "datetime | None" = None) -> date:
    """The last COMPLETED trading session as of `now`."""
    now = now or datetime.now()
    today = now.date()
    done_today = (nse_holidays.is_trading_day(today) and
                  (now.hour * 60 + now.minute) >= SESSION_DONE_MIN)
    return today if done_today else nse_holidays.last_trading_day(today)


def status(now: "datetime | None" = None) -> dict:
    have, want = db_day(), expected_day(now)
    return {"have": have, "want": want,
            "stale": have is None or have < want}


def report(now: "datetime | None" = None) -> dict:
    st = status(now)
    mark = "⚠  PURANA" if st["stale"] else "✅ latest"
    print(f"  🗄  5-min DB: newest {st['have']}  ·  chahiye {st['want']}  "
          f"·  {mark}")
    return st


#  Where the updater leaves the token it just minted, if it writes one.
UPDATER_TOKEN = (os.path.join(os.path.dirname(UPDATE_SCRIPT), "access_token.txt")
                 if UPDATE_SCRIPT else "")


def adopt_updater_token() -> bool:
    """
    Take the token update_db_5min just generated and make it ours.

    ── Why this is not a shortcut ──────────────────────────────────────────
    The updater and this project use the SAME Dhan account, and Dhan keeps
    exactly ONE live token per account — issuing a new one kills the old.  So
    the moment the updater logs in, our stored token is dead.  Logging in again
    right after cannot fix it either: Dhan throttles token generation to about
    one per two minutes and answers the extra attempts by dropping the TLS
    connection, which surfaces as WinError 10054 and reads like a network
    fault.  Measured 2026-08-11 — the update succeeded, then five login
    attempts failed in a row and the run died with a valid DB and no feed.

    So the second login is not made at all.  The token the updater just minted
    is for OUR account and is the freshest one in existence; it is copied into
    this project's own files and from then on nothing here reads that folder
    again.  Independence was never about avoiding one file — it was about not
    being at the mercy of another project's login schedule, and taking a token
    that was made seconds ago is the opposite of that.

    A separate Dhan account removes the whole problem; until then this is the
    only order that works.
    """
    try:
        import auto_login
    except Exception:
        return False
    if not UPDATER_TOKEN or not os.path.exists(UPDATER_TOKEN):
        return False
    try:
        fresh = open(UPDATER_TOKEN, encoding="utf-8").read().strip()
    except Exception:
        return False
    if not fresh or not auto_login.is_token_valid(fresh):
        return False

    mine = ""
    if os.path.exists(auto_login.TOKEN_FILE):
        try:
            mine = open(auto_login.TOKEN_FILE, encoding="utf-8").read().strip()
        except Exception:
            mine = ""
    if mine == fresh:
        return False

    try:
        with open(auto_login.TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(fresh)
    except Exception as e:
        print(f"  ⚠  token save nahi hua: {e}")
        return False
    print(f"  🔑 Updater ne abhi jo token banaya wahi le liya — dobara login "
          f"nahi karna padega (Dhan 2 min me ek hi deta hai)")
    return True


def run_update() -> bool:
    """Run the configured updater script and report whether the DB moved."""
    if not UPDATE_SCRIPT:
        print("  ℹ  FIVEMIN_UPDATER set nahi hai — 5-min DB refresh skip.")
        return False
    path = os.path.abspath(UPDATE_SCRIPT)
    if not os.path.exists(path):
        print(f"  ❌ nahi mila: {path}")
        return False
    before = db_day()
    print(f"\n  ▶  5-min candles Dhan se la raha hoon  ({UPDATE_SCRIPT})")
    print(f"  {'-' * 62}")
    t0 = time.time()
    #  cwd matters — that script resolves its DB and config relative to its own
    #  folder.  -u so its progress reaches this terminal as it happens.
    proc = subprocess.run([sys.executable, "-u", path],
                          cwd=os.path.dirname(path) or None)
    after = db_day()
    ok = proc.returncode == 0
    print(f"  {'-' * 62}")
    print(f"  {'✅' if ok else '❌'} update {round(time.time() - t0, 1)}s "
          f"(exit {proc.returncode}) — DB {before} → {after}")
    #  The updater logged in, so our token is dead now.  Adopt the one it just
    #  made rather than asking Dhan for another inside its throttle window.
    adopt_updater_token()
    return ok


def _last_attempt() -> "tuple[datetime | None, str | None]":
    """(when we last ran the updater, what the DB day was afterwards)."""
    try:
        import json
        with open(ATTEMPT_STAMP, encoding="utf-8") as f:
            b = json.load(f)
        return datetime.fromisoformat(b["at"]), b.get("got")
    except Exception:
        return None, None


def _note_attempt(got: "date | None") -> None:
    try:
        import json
        with open(ATTEMPT_STAMP, "w", encoding="utf-8") as f:
            json.dump({"at": datetime.now().isoformat(),
                       "got": got.isoformat() if got else None}, f)
    except Exception:
        pass


def _cooling_off(want: date, now: datetime) -> "int | None":
    """
    Minutes left before it is worth asking Dhan again, or None to go ahead.

    Only applies when the LAST attempt already failed to reach `want` — a run
    that succeeded, or one from a different day, says nothing about now.
    """
    at, got = _last_attempt()
    if at is None or at.date() != now.date():
        return None
    if got is not None and date.fromisoformat(got) >= want:
        return None                      # last try worked; nothing to cool off
    mins = (now - at).total_seconds() / 60.0
    left = RETRY_COOLDOWN_MIN - mins
    return int(left) + 1 if left > 0 else None


def ensure(now: "datetime | None" = None, allow_update: bool = True) -> dict:
    """
    Check, and update when stale.  Returns the status AFTER any update, with
    `updated` saying whether the script was actually run.

    A failed update is NOT fatal here.  Yesterday's bands are wrong-ish, but a
    heatmap on slightly stale data still beats no heatmap — the caller is told
    and prints a warning, so the state is visible rather than silent.
    """
    now = now or datetime.now()
    st = report(now)
    st["updated"] = False
    if not st["stale"]:
        return st
    if not allow_update:
        print(f"  ⏭  --no-update : purane data pe hi chal raha hoon")
        return st

    left = _cooling_off(st["want"], now)
    if left is not None:
        print(f"  ⏭  {RETRY_COOLDOWN_MIN - left + 1} minute pehle koshish ki "
              f"thi, {st['want']} ka data tab nahi aaya tha — "
              f"{left} min baad phir dekhunga.")
        print(f"     (Dhan aaj ka data band hone ke turant baad nahi deta — "
              f"10-Aug ko 16:40 pe aaya tha.)")
        return st

    ok = run_update()
    st = status(now)
    st["updated"] = True
    _note_attempt(st["have"])
    if st["stale"]:
        #  Right after the close this is EXPECTED, not a fault — Dhan simply
        #  has not published the session yet.  Saying "fail" there would send
        #  you looking for a bug that is not one.
        soon = (now.hour * 60 + now.minute) < 17 * 60
        if soon and ok:
            print(f"  ⏳ {st['want']} ka data Dhan pe abhi nahi aaya — "
                  f"thodi der baad chalana (aam taur pe ~16:30-17:00).")
            print(f"     Abhi {st['have']} ke data pe chal raha hoon.")
        else:
            print(f"  ⚠  DB abhi bhi {st['have']} pe hai, chahiye tha "
                  f"{st['want']} — prev close purane session ka rahega.")
            if not ok:
                print(f"     update script fail hua.  Upar ka output dekho.")
    else:
        print(f"  ✅ DB ab latest hai ({st['have']})")
    return st


def main() -> int:
    print(f"\n{'=' * 66}")
    print("  5-MIN DB STATUS")
    print(f"{'=' * 66}")
    st = (report() if "--check" in sys.argv
          else ensure(allow_update=True))
    print()
    return 0 if not st["stale"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
