# ─────────────────────────────────────────────────────────────────────────────
#  tunnel.py  —  public link, doosre network se dekhne ke liye
#
#  Copied from CRYPTO\tunnel.py — same approach, same ngrok binary.  LAN binding
#  (0.0.0.0) sirf ek hi WiFi cover karta hai; ye kisi bhi network se kholta hai.
#
#  ngrok agent ko child process ki tarah chalata hai aur uske apne local API
#  (127.0.0.1:4040) se public URL padh leta hai — koi pip package nahi chahiye.
#
#  ⚠  Link PUBLIC hai.  Jiske paas URL hoga wo heatmap dekh lega.  Us page pe
#     jaata kya hai: symbol, LTP, change%, day OHLC aur sector.  Na token, na
#     credentials, na koi order.  Phir bhi link kisi ke saath share mat karna.
#
#  Free tier ka URL har restart pe badal jaata hai; main.py naya URL print karta
#  hai.  Fixed URL chahiye to ngrok dashboard se ek static domain le lo aur
#  config.NGROK_DOMAIN me daal do.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import config

_API = "http://127.0.0.1:4040/api/tunnels"
_proc: "subprocess.Popen | None" = None


def _resolve_binary() -> "str | None":
    """config.NGROK_PATH if set, else whatever `ngrok` is on PATH."""
    p = getattr(config, "NGROK_PATH", "") or ""
    if p:
        return p if (os.path.isfile(p) or shutil.which(p)) else None
    return shutil.which("ngrok")


def _public_url(timeout: float = 20.0) -> "str | None":
    """Poll ngrok's local API until it reports a tunnel."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(_API, timeout=3) as r:
                data = json.load(r)
            for t in data.get("tunnels", []):
                if t.get("proto") == "https" and t.get("public_url"):
                    return t["public_url"]
            for t in data.get("tunnels", []):        # http-only fallback
                if t.get("public_url"):
                    return t["public_url"]
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


def start(port: int) -> "str | None":
    """
    Launch ngrok for `port`.  Returns the public URL, or None.

    An agent that is ALREADY running is reused rather than starting a second —
    the free tier caps concurrent sessions, so a second one would just fail.
    That also means starting the heatmap while CRYPTO's tunnel is up will hand
    back CRYPTO's URL, pointing at CRYPTO's port.  The check below catches that
    instead of printing a link to the wrong dashboard.
    """
    global _proc

    existing = _public_url(timeout=1.5)
    if existing:
        if _tunnel_port(existing) not in (None, port):
            print(f"        ⚠  ngrok pehle se chal raha hai par kisi aur port "
                  f"pe ({_tunnel_port(existing)}), is heatmap pe nahi.")
            print(f"           Usko band karo, ya heatmap ko usi port pe "
                  f"chalao.  Public link skip kar raha hoon.")
            return None
        return existing

    exe = _resolve_binary()
    if not exe:
        print("        ngrok nahi mila — config.py me NGROK_PATH set karo")
        return None

    cmd = [exe, "http", str(port), "--log=stdout"]
    domain = getattr(config, "NGROK_DOMAIN", "") or ""
    if domain:
        cmd += ["--domain", domain]
    try:
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"        ngrok start nahi hua: {e}")
        return None

    atexit.register(stop)

    url = _public_url()
    if not url:
        print("        ngrok chala par URL nahi mila — `ngrok config check` "
              "chala ke dekho (authtoken set hai?)")
    return url


def _tunnel_port(url: str) -> "int | None":
    """Which local port the running agent is forwarding to."""
    try:
        with urllib.request.urlopen(_API, timeout=3) as r:
            data = json.load(r)
        for t in data.get("tunnels", []):
            if t.get("public_url") == url:
                addr = (t.get("config") or {}).get("addr", "")
                return int(str(addr).rsplit(":", 1)[-1])
    except Exception:
        pass
    return None


def stop():
    global _proc
    if _proc and _proc.poll() is None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.PORT
    print(f"\n  ngrok -> port {port} ...")
    u = start(port)
    print(f"  {u or 'nahi bana'}\n")
    if u:
        print("  Ctrl-C se band.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop()
