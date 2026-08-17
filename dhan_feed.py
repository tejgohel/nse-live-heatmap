# ─────────────────────────────────────────────────────────────────────────────
#  dhan_feed.py  —  which Dhan account this heatmap uses, and a preflight
#
#  A Dhan account without the Data API add-on does NOT fail the WebSocket
#  handshake.  It accepts the socket, takes the subscribe, and closes — with no
#  error frame and no disconnect code.  From the client that is indistinguish-
#  able from a network blip, so a reconnect loop chases it forever.  Measured
#  against two accounts, one with the add-on and one without:
#
#      without add-on   /v2/marketfeed/ltp  401 {"806": "not Subscribed"}
#                       WebSocket           handshake OK, closed, 0 messages
#      with add-on      /v2/marketfeed/ltp  200
#                       WebSocket           ticks flowed, stayed up
#
#  So check_data_access() asks ONE REST question before any socket opens.  A 806
#  stops the run with a message you can act on, instead of a silent retry loop.
#
#      python dhan_feed.py      # every configured account's status
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

#  ── THIS PROJECT IS SELF-CONTAINED ──────────────────────────────────────────
#  Credentials come from your .env (see config.py) and the working token is
#  cached in access_token.txt.
#
#  Worth knowing: Dhan invalidates the previous token the moment a new one is
#  issued, so two programs logging in on the SAME account will keep killing
#  each other's feed.  If you run something else against this account, give
#  this one its own.
#
#  ── TO CHANGE THE DHAN ACCOUNT ──────────────────────────────────────────────
#  Edit ONE place: your .env — DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, and
#  optionally DHAN_PIN / DHAN_TOTP_SECRET.  Nothing here needs touching:
#  `client_id: None` means "whatever config says", so the number is never
#  written twice and the two copies cannot drift apart.  A clientId from one
#  account paired with a token from another is exactly the "connects, then
#  drops" failure this module exists to prevent.
#
#  Then check it before market open:
#      python dhan_feed.py
#  It must print OK.  A Dhan account WITHOUT the Data API add-on does not fail
#  the WebSocket — it accepts the socket and closes it silently — so that one
#  command is the difference between a working feed and a reconnect loop.
ACCOUNTS = {
    "heatmap": {
        "client_id": None,           # None -> take it from config (your .env)
        "module_dir": _HERE,
        "token_file": os.path.join(_HERE, "access_token.txt"),
        "note": "set DHAN_CLIENT_ID in .env to change the account",
    },
}

FEED_ACCOUNT = "heatmap"

#  ── Do NOT hammer this ──────────────────────────────────────────────────────
#  Dhan throttles token generation to roughly ONE PER TWO MINUTES, and when it
#  refuses it drops the TLS connection rather than answering — so the failure
#  arrives as WinError 10054 "connection forcibly closed" and looks exactly
#  like a network fault.  On 2026-08-11 that read as flakiness, the retries were
#  tightened to five tries 6-24s apart, and every single one failed: the retries
#  were the problem, all landing inside the same throttle window.
#
#  auto_login.generate_token() already retries 3x internally and recognises the
#  throttle and TOTP-rollover messages.  So this layer adds only ONE more try,
#  after a wait that actually clears the window.
LOGIN_ATTEMPTS = 2
LOGIN_RETRY_WAIT = 125      # seconds — past Dhan's ~2 minute throttle


_SESSION = None
_SESSION_LOCK = threading.Lock()
POOL_SIZE = 6


def session():
    """
    One pooled, retrying HTTP session for every Dhan REST call in this project.

    This machine resets TLS handshakes to api.dhan.co often — measured on
    2026-08-10, two of three FRESH connections died with WinError 10054 and
    succeeded immediately on retry.  Every request that opens its own connection
    pays that dice roll, which is how a four-try preflight managed to fail
    outright and stop a run that would have been fine.

    A keep-alive pool removes the handshake from all but the first call, and the
    connect-level Retry catches what still slips through.  `status=0` on
    purpose: HTTP 429 must reach the caller so prevclose's pacer can widen its
    gap — a silent urllib3 retry would hide the rate limit and make it worse.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(total=None, connect=4, read=2, status=0,
                          backoff_factor=0.6,
                          allowed_methods=frozenset(["POST", "GET"]))
            ad = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE,
                             max_retries=retry)
            s = requests.Session()
            s.mount("https://", ad)
            _SESSION = s
        return _SESSION


def _load_module(dirpath: str, name: str):
    """Import `name` from `dirpath` without leaving it on sys.path."""
    import importlib.util
    path = os.path.join(dirpath, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_heatmap_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stored_token(cfg: dict) -> str:
    """Whatever token is on disk for this account — token file first, then its
    config.py."""
    if cfg.get("token_file") and os.path.exists(cfg["token_file"]):
        try:
            t = open(cfg["token_file"], encoding="utf-8").read().strip()
            if t:
                return t
        except Exception:
            pass
    try:
        c = _load_module(cfg["module_dir"], "config")
        return getattr(c, "ACCESS_TOKEN", "") or ""
    except Exception:
        return ""


def _persist_token(cfg: dict, auto, token: str) -> str:
    """Write a fresh token to BOTH places and report which ones took."""
    where = []
    if cfg.get("token_file"):
        try:
            with open(cfg["token_file"], "w", encoding="utf-8") as f:
                f.write(token)
            where.append(os.path.basename(cfg["token_file"]))
        except Exception as e:
            print(f"  ⚠  token file likhne me dikkat: {e}")
    #  Deliberately NOT written into config.py: that file is tracked by git,
    #  and a token committed once is a token leaked forever. The cache file
    #  above is git-ignored, which is where a secret belongs.
    return ", ".join(where) if where else "nowhere (save failed)"


def _lasts_the_session(auto, token: str) -> bool:
    """
    Is this token still good at 15:30 today?

    ONE standard, applied everywhere a token is accepted.  The rule used to be
    split: the fallback after a failed login demanded a token that survives the
    session, but the "stored token is fine, skip the login" fast path only asked
    whether it was valid *right now*.  A token issued at 11:00 yesterday passes
    that weaker test at 09:05 and dies at 11:00 — the feed starts, the heatmap
    freezes mid-morning, and nothing says why.  Refusing it costs one login;
    accepting it costs half a session.

    BOTH halves are required, and the second is not optional dressing.
    auto_login.is_token_valid_until_market_close() only asks `exp > 15:30
    TODAY`, so after the close it compares against a time already past and an
    ALREADY-DEAD token sails through: at 19:46 a token that expired at 16:46
    still has exp > 15:30.  Verified with hand-built JWTs — that case returned
    True on its own.  `is_token_valid()` is what rules out the past; the
    market-close test is what rules out dying mid-session.

    On an evening run the market-close half is trivially satisfied, so a live
    token still costs no pointless login.
    """
    if not token:
        return False
    if not auto.is_token_valid(token):       # already expired — nothing else matters
        return False
    until_close = getattr(auto, "is_token_valid_until_market_close", None)
    if until_close is None:                  # older auto_login.py
        return True
    return bool(until_close(token))


def get_token(account: str = None, force_new: bool = False) -> "tuple[str, str]":
    """(client_id, access_token) for the feed account, refreshed only if needed."""
    account = account or FEED_ACCOUNT
    cfg = ACCOUNTS[account]
    auto = _load_module(cfg["module_dir"], "auto_login")

    #  The client id comes from config (i.e. your .env) — that is the account
    #  that actually signs the token, so it always wins.  `client_id: None`
    #  means ACCOUNTS never claimed one, which is the normal case and not a
    #  conflict; a mismatch against a value someone DID write down is worth
    #  shouting about.
    import config as _cfgmod
    real_id = getattr(_cfgmod, "CLIENT_ID", "") or cfg["client_id"]
    if cfg["client_id"] is None:
        cfg = dict(cfg, client_id=real_id)
    elif real_id != cfg["client_id"]:
        print(f"  ⚠  {account}: ACCOUNTS says {cfg['client_id']} but "
              f".env says {real_id} — going with .env.")
        cfg = dict(cfg, client_id=real_id)
    if not cfg["client_id"]:
        print(f"  ❌ {account}: no client id found — "
              f".env me DHAN_CLIENT_ID bharo (config.py dekho).")
        return "", ""

    if not force_new:
        token = _stored_token(cfg)
        if _lasts_the_session(auto, token):
            print(f"  🔑 {account} ({cfg['client_id']}) — stored token 15:30 "
                  f"— no login needed")
            return cfg["client_id"], token
        if token and auto.is_token_valid(token):
            print(f"  ⚠  {account} ({cfg['client_id']}) — stored token is valid now "
                  f"but dies before 15:30, generating a fresh one "
                  f"(otherwise the feed drops mid-session)")

    #  Two things make a single attempt unreliable and both clear on a retry:
    #  auth.dhan.co resets the odd TLS connection on this machine, and a TOTP
    #  generated at the edge of its 30-second window comes back "Invalid TOTP".
    for i in range(1, LOGIN_ATTEMPTS + 1):
        print(f"  🔑 {account} ({cfg['client_id']}) — login attempt "
              f"{i}/{LOGIN_ATTEMPTS}...")
        fresh = auto.generate_token()
        if fresh:
            saved = _persist_token(cfg, auto, fresh)
            print(f"  ✅ TOKEN BANA — attempt {i}/{LOGIN_ATTEMPTS} pe")
            print(f"     saved -> {saved}")
            return cfg["client_id"], fresh
        if i < LOGIN_ATTEMPTS:
            print(f"  ⚠  attempt {i} failed — waiting {LOGIN_RETRY_WAIT}s "
                  f"(Dhan issues one token per two minutes; retrying sooner "
                  f"gets you shut out instead)")
            time.sleep(LOGIN_RETRY_WAIT)

    #  Everything failed.  Fall back to the token on disk, but ONLY if it lasts
    #  through the session — one that dies at 11:00 would let the page start and
    #  then go stale mid-morning, which is worse than not starting.
    print(f"  ❌ NO TOKEN GENERATED — {LOGIN_ATTEMPTS} attempts failed")
    #  force_new means the caller ALREADY proved the stored token is dead —
    #  ensure_feed only sets it after Dhan answered 808 "invalid/superseded".
    #  Handing that same token back is guaranteed useless, and it read as a
    #  recovery ("the old token is valid till 15:30") while the run then failed
    #  on the very next check.  is_token_valid cannot see this: it reads the
    #  JWT's own expiry, and a superseded token's expiry is still in the future.
    if force_new:
        print(f"  ⚠  The old token has already been rejected (superseded), "
              f"so it is not offered back — only a fresh one will work.")
        return cfg["client_id"], ""
    stored = _stored_token(cfg)
    if stored:
        if _lasts_the_session(auto, stored):
            print(f"  ♻  Old token is valid till 15:30 — reusing it")
            return cfg["client_id"], stored
        if auto.is_token_valid(stored):
            print(f"  ⚠  Old token expires before 15:30 — not using it, it would "
                  f"drop the feed halfway through the day.")
        else:
            print(f"  ⚠  The old token has expired too.")
    else:
        print(f"  ⚠  No previous token found on disk.")
    #  No usable token.  Empty string, and every caller treats that as fatal:
    #  ensure_feed() returns None and main() exits.  Starting without one only
    #  buys a page that cannot update.
    return cfg["client_id"], ""


def headers(client_id: str, token: str) -> dict:
    return {"Accept": "application/json", "Content-Type": "application/json",
            "access-token": token, "client-id": client_id}


#  What the preflight concluded.  UNREACHABLE is deliberately NOT the same as
#  REFUSED: "Dhan said no" is a reason to stop, "we could not ask" is not.
OK = "ok"
REFUSED = "refused"
UNREACHABLE = "unreachable"


def check_data_access(client_id: str, token: str,
                      retries: int = 4) -> "tuple[str, str]":
    """
    Ask Dhan whether this account may receive market data, BEFORE a socket is
    opened.  Returns (verdict, human message).

        401 + code 806  -> the Data API add-on is not on this account
        401 + code 808  -> the token is dead (superseded or expired)
        200             -> data will actually flow
    """
    url = "https://api.dhan.co/v2/marketfeed/ltp"
    body = json.dumps({"NSE_EQ": [2885]})        # RELIANCE
    last = ""
    for _ in range(retries):
        try:
            r = session().post(url, headers=headers(client_id, token),
                               data=body, timeout=20)
        except Exception as e:
            last = f"network: {type(e).__name__}"
            time.sleep(3)
            continue
        if r.status_code == 200:
            return OK, "data access OK"
        txt = r.text[:200]
        if "806" in txt:
            return REFUSED, ("Data APIs not Subscribed — is account ka Dhan Data "
                             "the Data API add-on is not active.  Dhan web -> Profile "
                             "-> DhanHQ APIs to subscribe, or dhan_feed."
                             "FEED_ACCOUNT doosre account pe rakho.")
        if "808" in txt:
            return REFUSED, "token invalid/superseded — a fresh one is needed"
        return REFUSED, f"HTTP {r.status_code}: {txt}"
    return UNREACHABLE, f"could not reach Dhan ({last})"


def ensure_feed(account: str = None) -> "tuple[str, str] | None":
    """
    Token + a data subscription that Dhan did not refuse, or None with the
    reason printed.

    A REFUSED preflight stops the run — that is the whole point of asking.  An
    UNREACHABLE one does not: the network wobbled, which says nothing about the
    subscription, and killing the heatmap over a TLS reset would be a worse
    failure than the one being guarded against.  The socket's own exponential
    backoff caps at 60s, so proceeding cannot turn into the request storm that
    made this preflight necessary.
    """
    account = account or FEED_ACCOUNT
    cid, token = get_token(account)
    if not token:
        print(f"  ❌ No token found for {account} ({cid}).")
        return None
    verdict, msg = check_data_access(cid, token)
    if verdict == REFUSED and "token" in msg:
        print(f"  🔑 {msg} — generating a fresh one...")
        cid, token = get_token(account, force_new=True)
        verdict, msg = (check_data_access(cid, token) if token
                        else (REFUSED, msg))
    if verdict == REFUSED:
        print(f"\n  ❌ FEED WILL NOT RUN — {account} ({cid})")
        print(f"     {msg}")
        print(f"     The socket would open and then close, so it is not "
              f"being connected.\n")
        return None
    if verdict == UNREACHABLE:
        print(f"  ⚠  {account} ({cid}) — preflight could not run: {msg}")
        print(f"     That is a network answer, NOT a subscription one — "
              f"continuing.")
        return cid, token
    print(f"  🔑 Feed account {account} ({cid}) — data access OK")
    return cid, token


def feed_url(client_id: str, token: str) -> str:
    return (f"wss://api-feed.dhan.co?version=2&token={token}"
            f"&clientId={client_id}&authType=2")


def _report(allow_login: bool) -> None:
    """
    This project's account status.

    Read-only by default, and that is the point: get_token() LOGS IN when the
    stored token has expired, and a login invalidates the account's previous
    token server-side.  A "just checking" command must not do that.  Pass
    --login when you actually want a fresh token.
    """
    print(f"\n  Checking this project's Dhan account"
          f"{'' if allow_login else '  (read-only — no logins)'}\n")
    #  Dhan rate-limits marketfeed PER ACCOUNT, so a token is asked about once
    #  and the answer reused.  With one account this is trivial; it stays
    #  because a second entry would otherwise earn an 805 "Too many requests"
    #  that says nothing about the subscription and reads like a real failure.
    checked: "dict[str, tuple[str, str]]" = {}
    for name, cfg in ACCOUNTS.items():
        try:
            auto = _load_module(cfg["module_dir"], "auto_login")
        except Exception as e:
            print(f"  {name:<15} {'?':<12}  auto_login.py failed to load: {e}")
            continue
        cid = getattr(auto, "DHAN_CLIENT_ID", "") or cfg["client_id"] or "?"

        note = ""
        if allow_login:
            #  force_new, because --login is asked for precisely when the
            #  stored token is not working.  A superseded token still LOOKS
            #  valid — its JWT expiry is untouched — so without this the flag
            #  quietly returned the same dead token it was run to replace.
            cid, tok = get_token(name, force_new=True)
        else:
            tok = _stored_token(dict(cfg, client_id=cid))
            if tok and not auto.is_token_valid(tok):
                tok = ""
        if not tok:
            print(f"  {name:<15} {cid:<12}  no valid token — "
                  f"`python dhan_feed.py --login` se banega")
            continue

        if tok in checked:
            verdict, msg = checked[tok]
            note += "  (same token, not re-checked)"
        else:
            if checked:
                time.sleep(1.5)          # be gentle with the per-account limit
            verdict, msg = check_data_access(cid, tok)
            checked[tok] = (verdict, msg)
        mark = {OK: "OK  ", REFUSED: "FAIL", UNREACHABLE: "????"}[verdict]
        star = "  <- FEED_ACCOUNT" if name == FEED_ACCOUNT else ""
        print(f"  {name:<15} {cid:<12}  {mark}  {msg}{star}{note}")
    print()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _report(allow_login="--login" in sys.argv)
