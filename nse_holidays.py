# ─────────────────────────────────────────────────────────────────────────────
#  nse_holidays.py  —  NSE Trading Holiday Calendar
#
#  Used by db.py and daily_db.py to determine the last valid trading day.
#  When today (or a candidate day) is a weekend OR a listed NSE holiday,
#  _last_biz_day() steps back further to find the most recent trading session.
#
#  ⚠️  UPDATE THIS LIST EACH YEAR with the official NSE holiday schedule.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import date, timedelta

# ── NSE Trading Holidays 2026 ─────────────────────────────────────────────────
NSE_HOLIDAYS_2026: set[date] = {
    date(2026,  1, 15),   # Municipal Corporation Election - Maharashtra
    date(2026,  1, 26),   # Republic Day
    date(2026,  3,  3),   # Holi
    date(2026,  3, 26),   # Shri Ram Navami
    date(2026,  3, 31),   # Shri Mahavir Jayanti
    date(2026,  4,  3),   # Good Friday
    date(2026,  4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026,  4, 25),   # Dr. Baba Saheb Ambedkar Jayanti (observed / NSE holiday)
    date(2026,  5,  1),   # Maharashtra Day
    date(2026,  5, 28),   # Bakri Id
    date(2026,  6, 26),   # Muharram
    date(2026,  9, 14),   # Ganesh Chaturthi
    date(2026, 10,  2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),   # Dussehra
    date(2026, 11, 10),   # Diwali-Balipratipada
    date(2026, 11, 24),   # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),   # Christmas
}

# Master set — add future years here
NSE_HOLIDAYS: set[date] = set()
NSE_HOLIDAYS.update(NSE_HOLIDAYS_2026)


def is_trading_day(d: date) -> bool:
    """
    Returns True if `d` is a valid NSE trading day:
      • Not a Saturday (weekday == 5)
      • Not a Sunday  (weekday == 6)
      • Not a listed NSE holiday
    """
    if d.weekday() >= 5:       # Saturday=5, Sunday=6
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True


def session_day(ref: date | None = None) -> date:
    """
    The trading session whose prices are ON SCREEN right now — today if today
    trades, else the last session that did.

    ── Kyun ye alag function hai ───────────────────────────────────────────────
    `date.today()` aur "jo session dikh raha hai" ek hi cheez sirf trading day
    pe hote hain.  Shanivaar/Itwaar ko Dhan ka quote SHUKRAVAAR ka close deta
    hai, to page pe session Friday ka hai — chahe calendar Sunday bole.

    Ye farak chhupa hua hai aur exactly isi tarah tootta hai: 2026-08-16
    (Itwaar) ko `expected_prev_day()` ne `last_trading_day(Sunday)` = FRIDAY
    diya, aur LTP bhi Friday ka tha.  Friday minus Friday = 0, to poora heatmap
    +0.00% ho gaya.  Screen pe kuch bhi galat nahi lagta — bas sab flat.

    Isliye jahan bhi "aaj" ka matlab "jo session dikh raha hai" ho, wahan
    date.today() nahi, YE use hota hai.  Jahan matlab sach me calendar din ho
    (cache ki umar, "aaj download kiya kya"), wahan date.today() hi sahi hai.
    """
    d = ref or date.today()
    return d if is_trading_day(d) else last_trading_day(d)


def last_trading_day(ref: date | None = None) -> date:
    """
    Returns the most recent NSE trading day STRICTLY BEFORE `ref`.
    If `ref` is None, uses today's date.

    Handles all of: weekends, NSE holidays, and long holiday streaks.

    Examples (run date shown on left → result on right):
      Friday  24-Apr-2026 (today)   → Thursday 23-Apr-2026
      Monday  27-Apr-2026           → Friday   24-Apr-2026  ✅ (skips Sat+Sun)
      Tuesday 28-Apr-2026           → Monday   27-Apr-2026
      Saturday 25-Apr-2026          → Friday   24-Apr-2026
      Sunday  26-Apr-2026           → Friday   24-Apr-2026
      Tuesday 29-May-2026           → Monday   29-May-2026
        wait — Bakri Id is 28-May (Thu) → so last trading day = Wed 27-May-2026
    """
    d = (ref if ref is not None else date.today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d
