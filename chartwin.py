# ─────────────────────────────────────────────────────────────────────────────
#  chartwin.py  —  saare chart EK doosri-screen wali Chrome window me, naye tab
#
#  Kya chahiye tha: total DO Chrome window — ek screen pe heatmap, doosri screen
#  pe ek hi Chrome jisme har chart NAYE TAB me khule.
#
#  ── Browser ke andar se ye ho hi nahi sakta ─────────────────────────────────
#  `window.open(url, name, "left=..,top=..")` — koi bhi window feature dete hi
#  Chrome POPUP window banata hai, jisme tab strip hota hi nahi.  Us popup se
#  `_blank` kholo to Chrome naya popup ya kisi aur window me tab kholta hai, us
#  popup me nahi.  To "positioned window jisme tabs add hon" JS se possible
#  nahi hai.  Isliye ye kaam server karta hai.
#
#  ── Jo asal me kaam karta hai ───────────────────────────────────────────────
#  Chrome ka apna behaviour: ek ALAG `--user-data-dir` (profile) ke saath chalao
#  to wo apni alag window kholta hai.  Us profile ke saath dobara chalao to wo
#  chalti hui window me NAYA TAB kholta hai — bilkul wahi jo chahiye.
#
#      pehli baar : chrome --user-data-dir=<profile> --window-position=X,Y <url>
#      har baar   : chrome --user-data-dir=<profile> <url>      -> naya tab
#
#  ── Doosri screen kahan hai ─────────────────────────────────────────────────
#  Windows se seedha poocha jaata hai (EnumDisplayMonitors), koi guess nahi.
#  Ye zaroori tha: is machine pe doosra monitor UPAR hai (top = -1350), right me
#  nahi.  Browser-side ka "screen ke right edge ke aage" wala andaaza yahan
#  x=1920 nikaalta tha jahan koi screen hai hi nahi, aur Chrome window ko wapas
#  primary screen pe khheench leta tha — user ko wahi "usi screen pe khul raha
#  hai" dikhta tha.
#
#  ── Ek seemaa ──────────────────────────────────────────────────────────────
#  Browser aur server EK HI machine pe hone chahiye.  LAN ya ngrok se dekhne
#  wale ke liye ye chalega nahi (window is machine pe khulegi), isliye frontend
#  wahan normal tab wala rasta use karta hai.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time

#  ── config.py DELIBERATELY optional ────────────────────────────────────────
#  This file is one of the two that get handed to someone else so charts open
#  on THEIR machine (see chart_agent.py).  config.py must never travel with
#  them: auto_login writes the live Dhan ACCESS_TOKEN into it on every login,
#  and it also carries this machine's paths.  Sending it would be handing over
#  the trading account.
#
#  So config is imported if present and quietly replaced by defaults if not.
#  Nothing here reads anything secret — only which monitor, which Chrome, which
#  profile.
try:
    import config                                        # noqa: F401
except Exception:                                        # standalone agent
    class config:                                        # type: ignore
        CHART_MONITOR = -1
        CHROME_PATH = ""
        CHART_PROFILE = "same"
        AGENT_PORT = 7011

_HERE = os.path.dirname(os.path.abspath(__file__))

#  ── Kaunsi Chrome profile ───────────────────────────────────────────────────
#  config.CHART_PROFILE:
#    "same"     (default) teri normal Chrome profile — TradingView ka login,
#               bookmarks, chart layouts sab wahi.
#    "separate" alag profile (neeche wali dir).  Deterministic hai par ekdam
#               khaali — TradingView me dobara login karna padega.
#
#  "same" me ek constraint hai jise sambhalna padta hai: ek user-data-dir ka
#  ek hi Chrome instance chalta hai, aur `chrome <url>` us instance ki LAST
#  ACTIVE window me tab kholta hai.  Command line se "is window me kholo" bola
#  hi nahi ja sakta.  Isliye URL bhejne se pehle chart window ko foreground
#  kiya jaata hai — tab wahin girta hai.
PROFILE_DIR = os.path.join(_HERE, "_chartprofile")

_lock = threading.Lock()
#  Chart window ka handle.  Yahi "kahan tab kholna hai" ka jawaab hai.
_chart_hwnd = None


# ── Monitors ─────────────────────────────────────────────────────────────────

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def monitors() -> "list[dict]":
    """Every monitor's WORK area (taskbar hataake), Windows ke hisaab se."""
    if not sys.platform.startswith("win"):
        return []
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    out: "list[dict]" = []

    cb_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                 ctypes.POINTER(_RECT), ctypes.c_double)

    def _cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcWork
        out.append({"left": r.left, "top": r.top,
                    "width": r.right - r.left, "height": r.bottom - r.top,
                    "primary": bool(mi.dwFlags & 1)})
        return 1

    try:
        user32.EnumDisplayMonitors(0, 0, cb_type(_cb), 0)
    except Exception:
        return []
    return out


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_name(pid: int) -> str:
    """basename of the EXE behind `pid`, lowercased ('' if unavailable)."""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(h)


def _chrome_windows() -> "list[tuple]":
    """
    (hwnd, title, left, top, width, height) for every visible CHROME window.

    The window CLASS is not enough to identify Chrome: `Chrome_WidgetWin_1` is
    Chromium's class, so every Electron app uses it too — on this machine VS
    Code's windows matched, and would have been eligible for "the new window we
    just opened" and for having their command line read as a Chrome profile.
    So the owning process's EXE is checked as well.
    """
    if not sys.platform.startswith("win"):
        return []
    from ctypes import wintypes
    u = ctypes.windll.user32
    out: "list[tuple]" = []

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if "Chrome_WidgetWin" not in cls.value:
            return True
        ttl = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, ttl, 512)
        if not ttl.value:
            return True                  # invisible helper windows
        if _exe_name(_window_pid(hwnd)) not in ("chrome.exe", "msedge.exe"):
            return True                  # Electron app wearing Chromium's class
        rc = _RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rc))
        out.append((hwnd, ttl.value, rc.left, rc.top,
                    rc.right - rc.left, rc.bottom - rc.top))
        return True

    try:
        u.EnumWindows(proto(_cb), 0)
    except Exception:
        return []
    return out


SW_MAXIMIZE = 3
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def _alive(hwnd) -> bool:
    return bool(hwnd) and bool(ctypes.windll.user32.IsWindow(hwnd)) \
        and bool(ctypes.windll.user32.IsWindowVisible(hwnd))


def _focus(hwnd) -> bool:
    """
    Bring `hwnd` to the foreground.

    Windows refuses SetForegroundWindow from a process that does not own the
    current foreground window — which is exactly our case, the click happened
    in Chrome and this is a Python server.  AttachThreadInput briefly joins the
    two input queues, which is the long-standing way round that rule.

    SW_RESTORE is called ONLY when the window is minimised: on a maximised
    window it would un-maximise it, undoing the whole point.
    """
    u = ctypes.windll.user32
    try:
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        fg = u.GetForegroundWindow()
        t_fg = u.GetWindowThreadProcessId(fg, None)
        t_me = u.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if t_fg and t_me and t_fg != t_me:
            attached = bool(u.AttachThreadInput(t_fg, t_me, True))
        u.BringWindowToTop(hwnd)
        ok = bool(u.SetForegroundWindow(hwnd))
        if attached:
            u.AttachThreadInput(t_fg, t_me, False)
        return ok
    except Exception:
        return False


def _place_and_maximize(hwnd, mon: dict) -> bool:
    """Move the window onto `mon`, then let Windows maximise it there."""
    u = ctypes.windll.user32
    try:
        u.SetWindowPos(hwnd, 0, mon["left"], mon["top"],
                       mon["width"], mon["height"],
                       SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.25)
        u.ShowWindow(hwnd, SW_MAXIMIZE)
        return True
    except Exception:
        return False


def _wait_new_window(before: set, timeout: float = 12.0):
    """The Chrome window that appeared after we asked for one."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, _t, _l, _tp, _w, _h in _chrome_windows():
            if hwnd not in before:
                return hwnd
        time.sleep(0.3)
    return None


def _maximize_on(mon: dict, timeout: float = 10.0) -> bool:
    """
    Maximise the window we just placed on `mon`.

    --window-size was already the monitor's work area, so the window FILLS the
    screen — but it is not actually in the maximised state, and the two differ
    where it matters: a real maximise re-fits itself when the screen resolution
    changes or the taskbar moves, and it snaps flush instead of leaving the
    1-2px frame gap Chrome draws.  Since the two monitors here are different
    sizes (1920x1200 and 2400x1350), "fill whatever screen you land on" is what
    the user actually asked for.

    Chrome's own --start-maximized cannot be used: it maximises on the DEFAULT
    monitor and ignores --window-position, which puts the window back on the
    wrong screen.  So the window is placed first, then Windows maximises it —
    and Windows always maximises to the monitor the window is already on.

    Matched by exact geometry rather than PID: Chrome forks, the launched
    process is usually not the one that owns the window, and the rect we just
    asked for is unique enough.  Falls back to "a Chrome window centred on that
    monitor" if Chrome nudged the size.
    """
    if not sys.platform.startswith("win"):
        return False
    want = (mon["left"], mon["top"], mon["width"], mon["height"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = _chrome_windows()
        hit = None
        for hwnd, _t, l, t, w, h in wins:
            if (abs(l - want[0]) <= 4 and abs(t - want[1]) <= 4 and
                    abs(w - want[2]) <= 8 and abs(h - want[3]) <= 8):
                hit = hwnd
                break
        if hit is None:
            for hwnd, _t, l, t, w, h in wins:
                cx, cy = l + w // 2, t + h // 2
                if (mon["left"] <= cx < mon["left"] + mon["width"] and
                        mon["top"] <= cy < mon["top"] + mon["height"]):
                    hit = hwnd
                    break
        if hit is not None:
            try:
                ctypes.windll.user32.ShowWindow(hit, SW_MAXIMIZE)
                return True
            except Exception:
                return False
        time.sleep(0.4)
    return False


def _contains(mon: dict, x: int, y: int) -> bool:
    return (mon["left"] <= x < mon["left"] + mon["width"] and
            mon["top"] <= y < mon["top"] + mon["height"])


def chart_monitor(here: "tuple | None" = None) -> "dict | None":
    """
    Jis monitor pe chart window jaani chahiye.

    `here` = (x, y) jahan HEATMAP ki window hai (page khud batata hai).  Uske
    hisaab se wo monitor chuna jaata hai jispe heatmap NAHI hai.

    Pehle ye "jo primary nahi hai" chunta tha — jo sirf tab sahi hai jab
    heatmap primary pe ho.  Kisi dost ke setup me heatmap secondary pe ho to
    chart usi screen pe khul jaata, jo poori baat hi khatam kar deta hai.

    config.CHART_MONITOR se index pin bhi kar sakte ho.  Ek hi monitor ho to
    None — caller normal tab khol dega.
    """
    mons = monitors()
    if len(mons) < 2:
        return None
    idx = int(getattr(config, "CHART_MONITOR", -1))
    if 0 <= idx < len(mons):
        return mons[idx]
    if here:
        for m in mons:
            if not _contains(m, here[0], here[1]):
                return m
    for m in mons:
        if not m["primary"]:
            return m
    return mons[-1]


# ── Kaunsi Chrome profile me heatmap khula hai ───────────────────────────────

def _cmdline(pid: int) -> str:
    """Us process ki poori command line."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}",
             "get", "CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            if line.startswith("CommandLine="):
                return line[len("CommandLine="):].strip()
    except Exception:
        pass
    #  wmic is deprecated and already absent on some Windows 11 builds.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
             f".CommandLine"],
            capture_output=True, text=True, timeout=12).stdout
        return out.strip()
    except Exception:
        return ""


def _window_pid(hwnd) -> int:
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def profile_args(here: "tuple | None") -> "list[str]":
    """
    The Chrome flags that put a new window in the SAME profile the heatmap is
    open in.

    Chrome without --profile-directory opens whichever profile was last used —
    not necessarily the one you are looking at.  This machine alone has three
    (Default, Profile 4, Profile 5), so "it worked here" proves nothing.

    So the page reports where its own window is, the Chrome window at that spot
    is found, and ITS command line is read back for --profile-directory /
    --user-data-dir.  Whatever profile the heatmap is in, the chart joins it —
    on any machine, without configuring anything.
    """
    if not here or not sys.platform.startswith("win"):
        return []
    x, y = here
    best = None
    for hwnd, _t, l, t, w, h in _chrome_windows():
        if l <= x < l + w and t <= y < t + h:
            best = hwnd
            break
    if best is None:
        return []
    cmd = _cmdline(_window_pid(best))
    if not cmd:
        return []
    return _parse_profile_flags(cmd)


#  Both values routinely contain a SPACE — Chrome's profiles are literally
#  named "Profile 4", and the default user-data-dir ends in "\User Data".  A
#  plain \S+ therefore truncates them: "Profile 4" came back as "Profile", and
#  "...\Chrome\User Data" as "...\Chrome\User", both of which point at nothing.
#  Windows does not always quote these on the command line, so the value is
#  read up to the next flag / URL instead of up to the next space.
_RE_UDD = re.compile(
    r'--user-data-dir=(?:"([^"]+)"|(.+?))(?=\s+--|\s+https?://|\s*$)')
#  Profile names are a closed set in practice, so they can be matched exactly.
_RE_PROF = re.compile(
    r'--profile-directory=(?:"([^"]+)"|(Default|Profile \d+)|(\S+))')


def _parse_profile_flags(cmd: str) -> "list[str]":
    args: "list[str]" = []
    m = _RE_UDD.search(cmd)
    if m:
        args.append(f"--user-data-dir={(m.group(1) or m.group(2)).strip()}")
    m = _RE_PROF.search(cmd)
    if m:
        args.append("--profile-directory="
                    + (m.group(1) or m.group(2) or m.group(3)).strip())
    return args


# ── Chrome ───────────────────────────────────────────────────────────────────

def _chrome() -> "str | None":
    p = getattr(config, "CHROME_PATH", "") or ""
    if p and os.path.isfile(p):
        return p
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(
                  r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if os.path.isfile(c):
            return c
    return shutil.which("chrome")


def tv_url(symbol: str, interval: str = "5") -> str:
    """TradingView NSE ticker — '-' aur '&' underscore ban jaate hain."""
    tv = "NSE:" + str(symbol or "").replace("-", "_").replace("&", "_")
    from urllib.parse import quote
    return (f"https://www.tradingview.com/chart/?symbol={quote(tv)}"
            f"&interval={interval}")


def _spawn(cmd: list) -> bool:
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess,
                                               "DETACHED_PROCESS", 0))
        return True
    except Exception:
        return False


def open_chart(symbol: str, interval: str = "5",
               here: "tuple | None" = None) -> "tuple[bool, str]":
    """
    `symbol` ka chart doosri screen wali Chrome window me NAYE TAB me kholo.

    `here` = (x, y) jahan heatmap ki window hai.  Usse do cheezein tay hoti
    hain — kaunsa monitor (jispe heatmap NAHI hai) aur kaunsi Chrome profile
    (jisme heatmap khula hai).  Isi wajah se ye kisi bhi machine pe bina
    configure kiye chalta hai.
    """
    exe = _chrome()
    if not exe:
        return False, "Chrome nahi mila — config.CHROME_PATH set karo"
    mon = chart_monitor(here)
    if mon is None:
        return False, "sirf ek monitor dikh raha hai"

    url = tv_url(symbol, interval)
    separate = str(getattr(config, "CHART_PROFILE", "same")).lower() == "separate"
    #  A brand-new --user-data-dir triggers Chrome's first-run experience: a
    #  welcome tab plus a "make Chrome default" prompt, and that screen
    #  swallowed both the URL and --window-position when this was measured.
    #  These flags make a fresh profile behave like an established one.
    prof = ([f"--user-data-dir={PROFILE_DIR}", "--no-first-run",
             "--no-default-browser-check", "--disable-session-crashed-bubble"]
            if separate else profile_args(here))

    global _chart_hwnd
    with _lock:
        have = _alive(_chart_hwnd)

        if have:
            #  The window exists — put it in front FIRST.  Chrome drops a new
            #  tab into whichever window of that profile was last active, and
            #  there is no command-line way to name a target window, so this
            #  focus is what actually decides where the tab lands.
            _focus(_chart_hwnd)
            time.sleep(0.15)
            if not _spawn([exe] + prof + [url]):
                return False, "Chrome start nahi hua"
            return True, "naya tab khula"

        #  No window yet (first click, or the user closed it).
        before = {h for h, *_ in _chrome_windows()}
        if not _spawn([exe] + prof + ["--new-window", url]):
            return False, "Chrome start nahi hua"

    def _settle():
        global _chart_hwnd
        hwnd = _wait_new_window(before)
        if hwnd is None:
            return
        _place_and_maximize(hwnd, mon)
        with _lock:
            _chart_hwnd = hwnd

    #  Off the request thread: Chrome takes a second or two to create the
    #  window, and a click should not wait on that.
    threading.Thread(target=_settle, daemon=True).start()
    return True, (f"chart window khul rahi hai — {mon['width']}x{mon['height']}"
                  f" @ {mon['left']},{mon['top']}, maximized"
                  + ("  (alag profile)" if separate else "  (teri profile)"))


def reset() -> None:
    """Forget the chart window, so the next click makes a fresh one."""
    global _chart_hwnd
    with _lock:
        _chart_hwnd = None


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("\n  Monitors:")
    for i, m in enumerate(monitors()):
        star = "  <- primary" if m["primary"] else ""
        print(f"    [{i}] {m['width']}x{m['height']} @ "
              f"{m['left']},{m['top']}{star}")
    m = chart_monitor()
    print(f"\n  Chart monitor : {m}")
    print(f"  Chrome        : {_chrome()}")
    print(f"  Profile       : {PROFILE_DIR}")
    syms = sys.argv[1:] or ["RELIANCE"]
    for s in syms:
        ok, msg = open_chart(s)
        print(f"  {s:<12} {'✅' if ok else '❌'} {msg}")
        import time
        time.sleep(2.5)
    print()
