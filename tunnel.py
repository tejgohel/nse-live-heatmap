# ─────────────────────────────────────────────────────────────────────────────
#  tunnel.py  —  a public link, for viewing from another network
#
#  Same approach and same ngrok binary as the author's other projects. Binding
#  to the LAN (0.0.0.0) only covers one WiFi; this opens it from any network.
#
#  It runs the ngrok agent as a child process and reads the public URL from
#  ngrok's own local API (127.0.0.1:4040) — no pip package required.
#
#  ⚠  The link is PUBLIC. Anyone holding the URL can watch the heatmap. What
#     reaches that page: symbol, LTP, change%, day OHLC and sector. No token,
#     no credentials, no orders. Even so, do not share the link.
#
#  On the free tier the URL changes on every restart; main.py prints the new
#  one. If you want a fixed URL, take a static domain from the ngrok dashboard
#  and put it in config.NGROK_DOMAIN.
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
    That also means starting the heatmap while another project's tunnel is up
    back that project's URL, pointing at its port.  The check below catches it
    instead of printing a link to the wrong dashboard.
    """
    global _proc

    existing = _public_url(timeout=1.5)
    if existing:
        if _tunnel_port(existing) not in (None, port):
            print(f"        ⚠  ngrok is already running, but on a different port "
                  f"({_tunnel_port(existing)}), not this heatmap's.")
            print(f"           Stop it, or run the heatmap on that port. "
                  f"Skipping the public link.")
            return None
        return existing

    exe = _resolve_binary()
    if not exe:
        print("        ngrok not found — set NGROK_PATH in config.py")
        return None

    cmd = [exe, "http", str(port), "--log=stdout"]
    domain = getattr(config, "NGROK_DOMAIN", "") or ""
    if domain:
        cmd += ["--domain", domain]
    try:
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"        ngrok failed to start: {e}")
        return None

    atexit.register(stop)

    url = _public_url()
    if not url:
        print("        ngrok started but no URL came back — try `ngrok config "
              "check` (is the authtoken set?)")
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
    print(f"  {u or 'not created'}\n")
    if u:
        print("  Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop()
