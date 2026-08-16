"""
prev_close ka source clock+calendar se tay hota hai — ye us rule ka test.

Har case me do sawaal:
  want       : kaunse session ka close chahiye
  use_quote  : quote ka ohlc.close us session ka hai ya aaj ka ban chuka

    python test_prevclose_rule.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime

sys.stdout.reconfigure(encoding="utf-8")

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prevclose

#  (kab chala rahe ho, expected want, expected use_quote, kyun)
CASES = [
    ("2026-08-11 09:05", "2026-08-10", True,  "Tue subah — Monday ka close"),
    ("2026-08-11 00:47", "2026-08-10", True,  "Tue raat 00:47 — YAHI wo run tha"),
    ("2026-08-11 15:39", "2026-08-10", True,  "settle se 1 min pehle"),
    ("2026-08-11 15:41", "2026-08-10", False, "settle ke baad — quote aaj ka"),
    ("2026-08-11 20:00", "2026-08-10", False, "raat — quote aaj ka"),
    ("2026-08-10 19:47", "2026-08-07", False, "Mon shaam — quote Monday ka"),
    ("2026-08-15 11:00", "2026-08-14", True,  "Saturday — Friday ka close"),
    ("2026-08-16 20:00", "2026-08-14", True,  "Sunday raat — Friday ka close"),
    ("2026-08-17 09:05", "2026-08-14", True,  "Monday subah — Friday ka close"),
    ("2026-10-02 18:00", "2026-10-01", True,  "Gandhi Jayanti holiday"),
]


def main() -> int:
    print(f"\n  {'kab chalao':<20} {'want':<12} {'quote?':<7} kyun")
    print("  " + "-" * 74)
    bad = 0
    for when, want_s, use_q, why in CASES:
        now = datetime.fromisoformat(when)
        want = prevclose.expected_prev_day(now.date())
        ok_q, _msg = prevclose.quote_gives_prev_session(now.date(), now)
        good = (want.isoformat() == want_s) and (ok_q == use_q)
        bad += 0 if good else 1
        mark = "  " if good else "✗ "
        print(f"  {mark}{when:<18} {want}   "
              f"{'QUOTE' if ok_q else 'hist ':<7} {why}")
        if not good:
            print(f"      expected want={want_s} use_quote={use_q}")
    print(f"\n  failures: {bad}")
    print(f"  {'RULE OK' if bad == 0 else 'MISMATCH'}\n")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
