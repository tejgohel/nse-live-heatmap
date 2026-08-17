# ─────────────────────────────────────────────────────────────────────────────
#  chartwin.py  —  every chart as a new tab in ONE Chrome window on the
#                  second screen
#
#  The goal: exactly TWO Chrome windows — the heatmap on one screen, and on the
#  other screen a single Chrome in which every chart opens as a NEW TAB.
#
#  ── Why the browser cannot do this ──────────────────────────────────────────
#  `window.open(url, name, "left=..,top=..")` — the moment you pass any window
#  feature, Chrome makes a POPUP window, and a popup has no tab strip. Open
#  `_blank` from that popup and Chrome puts the tab in a new popup or in some
#  other window, never in the popup itself. So "a positioned window you can add
#  tabs to" is simply not reachable from JavaScript. Hence the server does it.
#
#  ── What actually works ─────────────────────────────────────────────────────
#  Chrome's own behaviour: launch it with a SEPARATE `--user-data-dir` (profile)
#  and it opens a window of its own. Launch it again with that same profile and
#  it opens a NEW TAB in the window already running — exactly what was wanted.
#
#      first time : chrome --user-data-dir=<profile> --window-position=X,Y <url>
#      every time : chrome --user-data-dir=<profile> <url>      -> new tab
#
#  ── Finding the second screen ───────────────────────────────────────────────
#  Windows is asked directly (EnumDisplayMonitors); nothing is guessed. That
#  mattered: on this machine the second monitor sits ABOVE (top = -1350), not to
#  the right. The browser-side guess of "just past the right edge of the screen"
#  produced x=1920, where no screen exists, so Chrome pulled the window back
#  onto the primary display — which looked to the user exactly like "it keeps
#  opening on the same screen".
#
#  ── One limitation ─────────────────────────────────────────────────────────
#  The browser and the server have to be on the SAME machine. For anyone viewing
#  over the LAN or through ngrok this cannot work (the window would open on the
#  server's machine), so the frontend falls back to an ordinary tab for them.
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

#  ── Which Chrome profile ────────────────────────────────────────────────────
#  config.CHART_PROFILE:
#    "same"     (default) your normal Chrome profile — TradingView login,
#               bookmarks and chart layouts all carry over.
#    "separate" its own profile (the directory below). Deterministic, but
#               completely empty — you have to log in to TradingView again.
#
#  "same" comes with a constraint that has to be worked around: one Chrome
#  instance per user-data-dir, and `chrome <url>` opens the tab in that
#  instance's LAST ACTIVE window. There is no command line for "open it in
#  THIS window". So the chart window is brought to the foreground before the
#  URL is sent — and the tab lands there.
PROFILE_DIR = os.path.join(_HERE, "_chartprofile")

_lock = threading.Lock()
#  Handle of the chart window. This is the answer to "where does the tab go".
_chart_hwnd = None


# ── Monitors ─────────────────────────────────────────────────────────────────

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def monitors() -> "list[dict]":
    """Every monitor's WORK area (taskbar excluded), as Windows reports it."""
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
    The monitor the chart window should go to.

    `here` = (x, y) of the HEATMAP window (the page reports its own position).
    The monitor chosen is the one the heatmap is NOT on.

    This used to pick "whichever is not primary", which is only correct while
    the heatmap happens to be on the primary. On a setup where the heatmap sits
    on the secondary, the chart opened on that same screen — defeating the
    whole point.

    config.CHART_MONITOR can pin an index instead. With only one monitor this
    returns None and the caller falls back to an ordinary tab.
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


# ── Which Chrome profile the heatmap is open in ──────────────────────────────

def _cmdline(pid: int) -> str:
    """That process's full command line."""
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
    """TradingView NSE ticker — '-' and '&' become underscores."""
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
    Open `symbol`'s chart as a NEW TAB in the second-screen Chrome window.

    `here` = (x, y) of the heatmap window. Two things follow from it — which
    monitor (the one the heatmap is NOT on) and which Chrome profile (the one
    the heatmap is open in). That is why this works on any machine with no
    configuration.
    """
    exe = _chrome()
    if not exe:
        return False, "Chrome not found — set config.CHROME_PATH"
    mon = chart_monitor(here)
    if mon is None:
        return False, "only one monitor detected"

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
                return False, "Chrome did not start"
            return True, "opened a new tab"

        #  No window yet (first click, or the user closed it).
        before = {h for h, *_ in _chrome_windows()}
        if not _spawn([exe] + prof + ["--new-window", url]):
            return False, "Chrome did not start"

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
    return True, (f"opening the chart window — {mon['width']}x{mon['height']}"
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
