# ─────────────────────────────────────────────────────────────────────────────
#  frontend_heatmap.py  —  the page
#
#  One Flask app, one port:
#      /                the heatmap
#      /api/snapshot    every stock's current row
#      /api/stream      SSE — snapshot first, then only what changed
#      /api/status
#
#  ── Why the stream sends deltas ─────────────────────────────────────────────
#  A full snapshot is ~210 rows, ~18 KB.  Pushed every second for a 6-hour
#  session that is close to 400 MB down the wire to do nothing but restate
#  prices that did not move.  So the browser gets the whole map once and then
#  only the stocks that actually ticked; on a quiet minute that is a handful.
#
#  ── Why the browser repaints on a timer, not on arrival ─────────────────────
#  Re-running the treemap on every message would relayout 210 tiles hundreds of
#  times a second.  Messages land in a buffer and a single requestAnimationFrame
#  pass applies them — the numbers stay live, the layout stops thrashing.
#
#  ── The colours are the user's rule, verbatim ───────────────────────────────
#      >= +2%          green            <= -2%          red
#      +0.5 .. +2      light green      -2 .. -0.5      light red
#      +0.01 .. +0.5   whitish green    -0.5 .. -0.01   whitish red
#      |chg| < 0.01    flat grey
#  and the TOP_N gainers / losers wear a DARK shade on top of that.  Dark is a
#  rank, not a threshold, because the top gainer is +11% on one day and +2.5%
#  on the next — see the note in config.py.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import queue
import socket
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify

import config

_rows: "dict[str, dict]" = {}
_subscribers: "list[queue.Queue]" = []
_status = {"text": "Starting...", "live": False}
_lock = threading.Lock()

app = Flask(__name__)
app.config["ENV"] = "production"


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    settings = {
        "strong": config.BAND_STRONG,
        "mild": config.BAND_MILD,
        "flat": config.BAND_FLAT,
        "topN": config.TOP_N,
        "minExtreme": config.MIN_EXTREME_PCT,
    }
    return (_HTML.replace("__SETTINGS__", json.dumps(settings))
                 .replace("__AGENTPORT__", str(getattr(config, "AGENT_PORT",
                                                       7011))))


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(_status)


@app.route("/api/snapshot")
def api_snapshot():
    with _lock:
        return jsonify(list(_rows.values()))


@app.route("/api/chart")
def api_chart():
    """
    Open `symbol` on the OTHER monitor, as a new tab in one dedicated Chrome
    window.  See chartwin.py for why this cannot be done from the browser.

    Only meaningful for a browser on THIS machine — the window opens on the
    server's desktop.  A LAN or ngrok visitor is told so and the page falls
    back to a normal tab, rather than silently opening a window nobody sees.
    """
    from flask import request
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"ok": False, "msg": "symbol required"}), 400
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"ok": False, "remote": True,
                        "msg": "opened from another device — will use a normal tab"}), 200
    try:
        import chartwin
        ok, msg = chartwin.open_chart(symbol, here=_here(request))
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    return jsonify({"ok": ok, "msg": msg,
                    "url": _tv_url(symbol)}), 200


def _here(request) -> "tuple | None":
    """
    Where the heatmap's own window sits, as the page reports it.

    This one pair of numbers is what makes the feature portable: the server
    uses it to pick the monitor the heatmap is NOT on, and to read back which
    Chrome profile that window belongs to.  Without it both are guesses that
    only happen to be right on the machine this was built on.
    """
    try:
        x = int(float(request.args["sx"]))
        y = int(float(request.args["sy"]))
    except (KeyError, TypeError, ValueError):
        return None
    return x, y


@app.route("/api/screens")
def api_screens():
    """What monitors this machine has — the toolbar shows it so the state is
    visible before you click anything."""
    from flask import request
    local = request.remote_addr in ("127.0.0.1", "::1", "localhost")
    try:
        import chartwin
        here = _here(request)
        mons = chartwin.monitors()
        target = chartwin.chart_monitor(here)
        chrome = bool(chartwin._chrome())
        prof = chartwin.profile_args(here)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}",
                        "local": local})
    return jsonify({"ok": bool(target) and chrome and local, "local": local,
                    "count": len(mons), "monitors": mons, "target": target,
                    "chrome": chrome, "profile": prof})


def _tv_url(symbol: str) -> str:
    from urllib.parse import quote
    tv = "NSE:" + str(symbol or "").replace("-", "_").replace("&", "_")
    return f"https://www.tradingview.com/chart/?symbol={quote(tv)}&interval=5"


@app.route("/api/stream")
def api_stream():
    """SSE: status, then the full map, then only the rows that change."""
    def generate():
        q: "queue.Queue" = queue.Queue()
        with _lock:
            _subscribers.append(q)
            first = list(_rows.values())
            cur = dict(_status)
        try:
            yield _sse({"type": "status", **cur})
            yield _sse({"type": "snapshot", "rows": first})
            while True:
                try:
                    yield _sse(q.get(timeout=25))
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ── Public API ───────────────────────────────────────────────────────────────

def publish(rows: "list[dict]") -> None:
    """Merge rows into the served state and push them to every open page."""
    if not rows:
        return
    with _lock:
        for r in rows:
            _rows[r["sid"]] = r
        payload = {"type": "update", "rows": rows}
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


def set_status(text: str, live: bool = False) -> None:
    with _lock:
        _status["text"] = text
        _status["live"] = live
        payload = {"type": "status", "text": text, "live": live}
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


def start_server(port: int = None) -> None:
    port = port or config.PORT
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def _run():
        app.run(host="0.0.0.0", port=port, debug=False,
                use_reloader=False, threaded=True)

    threading.Thread(target=_run, daemon=True).start()
    time.sleep(1.0)


def open_browser(port: int = None) -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{port or config.PORT}/")
    except Exception:
        pass


def lan_ip() -> str:
    """LAN IP another PC on the same network should point its browser at."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packet sent — picks the interface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ─────────────────────────────────────────────────────────────────────────────

_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>NSE F&O Heatmap — Live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 /* The page is exactly one screen: header, then the map fills whatever is left.
    A fixed `calc(100vh - N)` was wrong on both ends — it wasted 46px on a wide
    window and overflowed the moment the toolbar wrapped to a second line. */
 html,body{height:100%}
 body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:#eef1f4;
      color:#0f172a;padding:10px;line-height:1.25;
      display:flex;flex-direction:column;overflow:hidden}
 h1{font-size:1.02rem;font-weight:700}
 h1 em{font-style:normal;color:#2563eb}
 .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;
      flex:0 0 auto}
 .pill{padding:5px 11px;border-radius:20px;font-size:.76rem;font-weight:600;
       background:#fff;border:1px solid #dbe2ea;color:#334155;white-space:nowrap}
 .dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;display:inline-block;
      margin-right:6px;vertical-align:middle}
 .dot.live{background:#22c55e;box-shadow:0 0 8px #22c55e}
 .up{color:#15803d}.dn{color:#b91c1c}
 input,select{background:#fff;border:1px solid #cbd5e1;color:#0f172a;padding:5px 9px;
      border-radius:7px;font-size:.78rem;font-weight:600}
 input{min-width:140px;font-weight:400}
 input:focus,select:focus{outline:none;border-color:#3b82f6}

 /* ── the map ─────────────────────────────────────────────────────────────
    Tiles are absolutely positioned from the treemap, so the container needs a
    real measured height — `flex:1` gives it one, and `min-height:0` is what
    stops a flex item from refusing to shrink below its content. */
 #map{position:relative;width:100%;flex:1;min-height:0;
      background:#fff;border:1px solid #dbe2ea;border-radius:8px;overflow:hidden}
 .tile{position:absolute;overflow:hidden;cursor:pointer;z-index:1;
       border:1px solid rgba(255,255,255,.85);
       display:flex;flex-direction:column;align-items:center;justify-content:center;
       text-align:center;padding:1px;
       transition:left .35s ease,top .35s ease,width .35s ease,height .35s ease,
                  background-color .45s ease}
 .tile:hover{outline:2px solid #1e293b;outline-offset:-2px;z-index:5}
 .tile .s{font-weight:700;line-height:1.05;white-space:nowrap}
 .tile .p{font-weight:600;line-height:1.15;opacity:.92;white-space:nowrap;
          font-variant-numeric:tabular-nums}
 .tile.flash{animation:fl .6s ease-out}
 @keyframes fl{0%{filter:brightness(1.45)}100%{filter:brightness(1)}}

 .legend{display:flex;gap:0;align-items:center;margin-left:auto;font-size:.68rem;
         font-weight:700;border-radius:6px;overflow:hidden;border:1px solid #dbe2ea}
 .legend span{padding:4px 8px;color:#fff;white-space:nowrap}
 .legend span.lt{color:#334155}

 .pill.chk{display:inline-flex;align-items:center;gap:7px;cursor:pointer;
           user-select:none}
 .pill.chk input{margin:0;cursor:pointer;width:14px;height:14px}
 .pill.chk #scrn{font-size:.7rem;color:#64748b;
   font-variant-numeric:tabular-nums}
 /* ── Sector grouping ─────────────────────────────────────────────────────
    Each sector becomes its own block with a label bar, and the stocks are laid
    out inside it — so a sector reads as a shape, not as a colour you have to
    hunt for.  The block sits on a dark ground with a small inset so the groups
    separate without needing gaps between the tiles themselves.

    z-index matters: sector blocks are appended to the map AFTER the tiles, so
    without it they paint over everything and the map is one navy rectangle. */
 .sect-box{position:absolute;background:#0f172a;border-radius:5px;
           overflow:hidden;z-index:0;
           transition:left .35s ease,top .35s ease,width .35s ease,height .35s ease}
 .sect-lbl{position:absolute;left:0;right:0;top:0;color:#e2e8f0;font-weight:800;
           letter-spacing:.03em;text-transform:uppercase;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis;text-align:center;
           padding:0 4px;pointer-events:none}
 .sect-lbl b{color:#94a3b8;font-weight:600;margin-left:5px}
 #empty{position:absolute;inset:0;display:none;align-items:center;
        justify-content:center;color:#94a3b8;font-size:.9rem;font-weight:600;
        text-align:center;padding:20px}
 #empty.on{display:flex}
</style></head><body>

<div class="bar">
 <h1>NSE <em>F&amp;O</em> Heatmap</h1>
 <span class="pill"><span id="dot" class="dot"></span><span id="status">Starting...</span></span>
 <span class="pill">▲ <span id="nup" class="up">0</span> &nbsp;▼ <span id="ndn" class="dn">0</span></span>
 <input id="q" placeholder="SEARCH SYMBOLS...">
 <select id="sec"><option value="">All sectors</option></select>
 <select id="size">
  <option value="equal">Size: Equal</option>
  <option value="turnover">Size: Turnover</option>
  <option value="move">Size: Move</option>
 </select>
 <label class="pill chk" title="Group the stocks into sector blocks — each block
is labelled with its name and how many stocks it holds.">
  <input type="checkbox" id="group"> Sector wise
 </label>
 <label class="pill chk" id="twoscr-lbl" title="Chart doosri screen pe, ek hi
Chrome window me, har chart naye tab me.">
  <input type="checkbox" id="twoscr"> ⧉ Chart 2nd screen
  <span id="scrn"></span>
 </label>
 <span class="legend" id="legend"></span>
</div>

<div id="map"><div id="empty"></div></div>

<script>
const S = __SETTINGS__;

/* ── Colours ──────────────────────────────────────────────────────────────
   Nine steps.  DARK is applied by rank (top N movers), everything else by the
   band the change% falls in.  Text colour flips on the pale steps so the
   symbol stays readable. */
const C = {
  darkUp:  {bg:'#0a5d2b', fg:'#ffffff'},
  up:      {bg:'#2fa35e', fg:'#ffffff'},
  midUp:   {bg:'#86d4a5', fg:'#0f2f1d'},
  paleUp:  {bg:'#d8f2e2', fg:'#14532d'},
  flat:    {bg:'#f1f3f5', fg:'#475569'},
  paleDn:  {bg:'#fbdedd', fg:'#7f1d1d'},
  midDn:   {bg:'#f09b9b', fg:'#3d1010'},
  dn:      {bg:'#e0453f', fg:'#ffffff'},
  darkDn:  {bg:'#8f1210', fg:'#ffffff'},
  none:    {bg:'#e2e8f0', fg:'#94a3b8'}
};

function bandOf(p){
  if(p === null || p === undefined || isNaN(p)) return 'none';
  if(p >=  S.strong) return 'up';
  if(p >=  S.mild)   return 'midUp';
  if(p >=  S.flat)   return 'paleUp';
  if(p >  -S.flat)   return 'flat';
  if(p >  -S.mild)   return 'paleDn';
  if(p >  -S.strong) return 'midDn';
  return 'dn';
}

(function legend(){
  const el = document.getElementById('legend');
  const items = [
    ['darkUp','Top'], ['up','≥'+S.strong+'%'], ['midUp',S.mild+'–'+S.strong],
    ['paleUp','0–'+S.mild], ['flat','0'], ['paleDn','0–'+S.mild],
    ['midDn',S.mild+'–'+S.strong], ['dn','≤-'+S.strong+'%'], ['darkDn','Top']
  ];
  el.innerHTML = items.map(function(it){
    const c = C[it[0]];
    const pale = (it[0]==='midUp'||it[0]==='paleUp'||it[0]==='flat'||
                  it[0]==='paleDn'||it[0]==='midDn');
    return '<span class="'+(pale?'lt':'')+'" style="background:'+c.bg+
           (pale?';color:'+c.fg:'')+'">'+it[1]+'</span>';
  }).join('');
})();

/* ── Squarified treemap ───────────────────────────────────────────────────
   Rows are laid along the SHORTER side of the remaining rectangle, which is
   what keeps tiles near-square.  On a wide screen the short side is the
   height, so the map fills in columns left to right — gainers first, losers
   last, because the list is sorted by change% before it gets here. */
function worstRatio(row, sum, side){
  const thick = sum / side;
  let w = 0;
  for(let i=0;i<row.length;i++){
    const len = row[i].a / thick;
    const r = Math.max(thick/len, len/thick);
    if(r > w) w = r;
  }
  return w;
}

function squarify(items, x, y, w, h){
  const out = [];
  let rest = items.slice();
  while(rest.length){
    if(rest.length === 1 || w <= 0.5 || h <= 0.5){
      const each = h / rest.length;
      for(let i=0;i<rest.length;i++)
        out.push(Object.assign({}, rest[i], {x:x, y:y+i*each, w:w, h:each}));
      break;
    }
    const vertical = w >= h;             // build a column when it is wide
    const side = vertical ? h : w;
    const row = [];
    let sum = 0, best = Infinity;
    while(rest.length){
      const cand = sum + rest[0].a;
      const r = worstRatio(row.concat([rest[0]]), cand, side);
      if(row.length === 0 || r <= best){
        row.push(rest.shift()); sum = cand; best = r;
      } else break;
    }
    const thick = sum / side;
    let off = 0;
    for(let i=0;i<row.length;i++){
      const len = row[i].a / thick;
      if(vertical) out.push(Object.assign({}, row[i], {x:x, y:y+off, w:thick, h:len}));
      else         out.push(Object.assign({}, row[i], {x:x+off, y:y, w:len, h:thick}));
      off += len;
    }
    if(vertical){ x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}

/* ── State ────────────────────────────────────────────────────────────────
   `rows` is the whole universe keyed by security id; `tiles` holds the live
   DOM node per id so an update mutates a node instead of rebuilding the map. */
const rows = {};
const tiles = {};
const sectBoxes = {};          // sector -> its background block + label
let pending = false, needLayout = true, lastKey = '';
//  False until the opening snapshot has been drawn, so a page load does not
//  flash every tile at once.
let ready = false;

function esc(s){ return String(s==null?'':s)
 .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function visible(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const sec = document.getElementById('sec').value;
  const out = [];
  for(const sid in rows){
    const r = rows[sid];
    if(q && r.symbol.toLowerCase().indexOf(q) < 0) continue;
    if(sec && (r.sector||'') !== sec) continue;
    out.push(r);
  }
  //  Nulls last: a stock with no previous close has no colour to sort by, and
  //  burying it beats letting it sit between the gainers and the losers.
  out.sort(function(a,b){
    const x = (a.pct===null||a.pct===undefined) ? -1e9 : a.pct;
    const y = (b.pct===null||b.pct===undefined) ? -1e9 : b.pct;
    return y - x;
  });
  return out;
}

function weightOf(r, mode){
  if(mode === 'turnover') return Math.max(r.turnover || 0, 1);
  if(mode === 'move')     return Math.max(Math.abs(r.pct || 0), 0.05);
  return 1;
}

/* ── Layouts ──────────────────────────────────────────────────────────────
   Both return [{sid, x, y, w, h}] in MAP coordinates, so everything
   downstream — tile creation, fitText, removal — is identical either way. */
function scaled(list, mode, area){
  let total = 0;
  const items = list.map(function(r){
    const wgt = weightOf(r, mode); total += wgt;
    return {sid: r.sid, a: wgt};
  });
  const k = area / (total || 1);
  items.forEach(function(it){ it.a *= k; });
  return items;
}

function layoutFlat(list, mode, W, H){
  return squarify(scaled(list, mode, W * H), 0, 0, W, H);
}

/*  Sector view: squarify the SECTORS first, then each sector's stocks inside
    its own rectangle.  A sector block is sized by the sum of its stocks'
    weights, so its area really is the sum of its parts — anything else and the
    picture lies about how big a sector is.

    A label bar is taken off the top of each block, capped at a fraction of the
    block height so a small sector does not end up all label and no stocks, and
    dropped entirely when the block is too short to carry text.  */
const LBL_H = 15, LBL_MIN = 9, PAD = 3;

function layoutBySector(list, mode, W, H){
  const bySec = {};
  list.forEach(function(r){
    const s = r.sector || '—';
    (bySec[s] = bySec[s] || []).push(r);
  });
  const secs = Object.keys(bySec).map(function(s){
    let wgt = 0;
    bySec[s].forEach(function(r){ wgt += weightOf(r, mode); });
    return {sid: s, a: wgt, rowsIn: bySec[s]};
  });
  //  Biggest sector first, so the squarifier gives it the roomiest corner.
  secs.sort(function(a, b){ return b.a - a.a; });
  let total = 0;
  secs.forEach(function(s){ total += s.a; });
  const k = (W * H) / (total || 1);
  secs.forEach(function(s){ s.a *= k; });
  const boxes = squarify(secs, 0, 0, W, H);

  const seen = {}, out = [];
  boxes.forEach(function(b){
    seen[b.sid] = 1;
    const lblH = (b.h > 46) ? Math.min(LBL_H, b.h * 0.16) : 0;
    let e = sectBoxes[b.sid];
    if(!e){
      const box = document.createElement('div');
      box.className = 'sect-box';
      const lbl = document.createElement('div');
      lbl.className = 'sect-lbl';
      box.appendChild(lbl);
      document.getElementById('map').appendChild(box);
      e = sectBoxes[b.sid] = {box: box, lbl: lbl};
    }
    e.box.style.left = b.x + 'px';   e.box.style.top = b.y + 'px';
    e.box.style.width = b.w + 'px';  e.box.style.height = b.h + 'px';
    const fs = Math.max(LBL_MIN,
                        Math.min(11, b.w / (b.sid.length + 5) / 0.62));
    e.lbl.style.display = lblH ? '' : 'none';
    e.lbl.style.height = lblH + 'px';
    e.lbl.style.lineHeight = lblH + 'px';
    e.lbl.style.fontSize = fs.toFixed(1) + 'px';
    e.lbl.innerHTML = esc(b.sid) + '<b>' + b.rowsIn.length + '</b>';

    //  Tiles live in MAP space, not inside the block, so the block's offset is
    //  added back here.
    const ix = b.x + PAD, iy = b.y + (lblH || PAD);
    const iw = Math.max(b.w - PAD * 2, 2);
    const ih = Math.max(b.h - (lblH || PAD) - PAD, 2);
    squarify(scaled(b.rowsIn, mode, iw * ih), ix, iy, iw, ih)
      .forEach(function(t){ out.push(t); });
  });
  for(const s in sectBoxes){
    if(!seen[s]){ sectBoxes[s].box.remove(); delete sectBoxes[s]; }
  }
  return out;
}

function render(){
  const map = document.getElementById('map');
  const W = map.clientWidth, H = map.clientHeight;
  if(W < 10 || H < 10) return;

  const list = visible();
  const mode = document.getElementById('size').value;

  //  Rank decides the dark shade.  Only names that already cleared the strong
  //  band qualify, so a flat day does not crown a +0.3% stock.
  const dark = {};
  const withPct = list.filter(function(r){ return r.pct !== null && r.pct !== undefined; });
  withPct.slice(0, S.topN).forEach(function(r){
    if(r.pct >= S.minExtreme) dark[r.sid] = 'darkUp'; });
  withPct.slice(-S.topN).forEach(function(r){
    if(r.pct <= -S.minExtreme) dark[r.sid] = 'darkDn'; });

  let up = 0, dn = 0;
  withPct.forEach(function(r){ if(r.pct > 0) up++; else if(r.pct < 0) dn++; });
  document.getElementById('nup').textContent = up;
  document.getElementById('ndn').textContent = dn;


  const empty = document.getElementById('empty');
  if(!list.length){
    empty.classList.add('on');
    empty.textContent = 'No stocks match this filter.';
  } else {
    empty.classList.remove('on');
  }

  const grouped = document.getElementById('group').checked;

  //  Relayout only when the SET or the ORDER changed.  A price tick that does
  //  not reorder anything just repaints, which is why the map does not crawl.
  const key = mode + '|' + (grouped ? 'g' : 'f') + '|' +
              list.map(function(r){ return r.sid; }).join(',');
  const relayout = needLayout || key !== lastKey;
  lastKey = key; needLayout = false;

  if(relayout){
    //  Remove the TILES, not the container's contents — #empty lives inside
    //  #map and an innerHTML wipe would take the "nothing matched" message
    //  with it, leaving a blank white box that looks like a crash.
    if(!list.length){
      for(const k in tiles){ tiles[k].remove(); delete tiles[k]; }
      for(const k in sectBoxes){ sectBoxes[k].box.remove(); delete sectBoxes[k]; }
      return;
    }
    const laid = grouped ? layoutBySector(list, mode, W, H)
                         : layoutFlat(list, mode, W, H);

    if(!grouped){
      for(const k in sectBoxes){ sectBoxes[k].box.remove(); delete sectBoxes[k]; }
    }
    const keep = {};
    laid.forEach(function(t){ keep[t.sid] = t; });
    for(const sid in tiles){
      if(!keep[sid]){ tiles[sid].remove(); delete tiles[sid]; }
    }
    laid.forEach(function(t){
      let el = tiles[t.sid];
      if(!el){
        el = document.createElement('div');
        el.className = 'tile';
        el.dataset.sid = t.sid;
        el.innerHTML = '<div class="s"></div><div class="s s2"></div>' +
                       '<div class="p"></div>';
        map.appendChild(el);
        tiles[t.sid] = el;
      }
      el.style.left = t.x + 'px'; el.style.top = t.y + 'px';
      el.style.width = t.w + 'px'; el.style.height = t.h + 'px';
      //  Kept so paint() can re-fit without a full relayout.
      el._w = t.w; el._h = t.h;
      fitText(el, rows[t.sid], t.w, t.h);
    });
  }

  list.forEach(function(r){
    const el = tiles[r.sid];
    if(!el) return;
    paint(el, r, dark[r.sid]);
  });
}

/* Type is sized to the LONGEST WORD the tile has to hold, not to the tile
   alone.  Sizing on width only clipped every long name — a 59px tile rendered
   "POWERINDIA" as "OWERINDI", which reads as a broken feed rather than a tight
   fit.  CHAR_W is the average glyph advance as a fraction of font-size for this
   bold sans at these sizes; the digits line is narrower because it is mostly
   figures.  Below MIN_FS a label is unreadable anyway, so it is dropped
   instead of shrunk further. */
const CHAR_W = 0.63, CHAR_W_NUM = 0.58, MIN_FS = 6.5, MAX_FS = 13;
//  The symbol is drawn SMALLER than the size it was fitted at.  Fitting finds
//  the largest type that would not overflow; drawing at that size filled every
//  tile edge to edge and the map read as a wall of text.  The percentage is
//  then capped at the symbol's size so the name never ends up the smaller of
//  the two.
const SYM_SCALE = 0.90;

/* Where a long symbol breaks when it has to go onto two lines.
   Preference order: a real separator, then a known word ending, then the
   middle.  The word endings are what turn UNIONBANK into UNION / BANK instead
   of UNIONB / ANK — a mechanical midpoint split reads as a typo. */
const SUFFIX = ['HOLDINGS','FINANCE','AIRPORT','HOUSING','UNILVR','MOTOCO',
                'CONSUM','FIRSTB','CEMENT','PHARMA','ENERGY','MOTORS','HEALTH',
                'INDBK','PRULI','TOWER','FORGE','INDIA','STEEL','POWER','PORTS',
                'MOTOR','PETRO','PHARM','CEMCO','ELXSI','GREEN','SOLAR','INFRA',
                'MINDA','HOTEL','LABS','FORG','ALCO','ZINC','WIND','ALUM','LIFE',
                'TECH','CHEM','AUTO','GRID','PROP','CORP','AGRO','MART','FOOD',
                'HOSP','RLTY','ENER','INDS','DSPR','FIN','CAP','IND','GAS','CEM',
                'LTD','BZR','OFS','BNK','AMC','LAB','LEY','OIL']
    //  LONGEST FIRST, always: otherwise 'IND' swallows INDIA and 'PHARM'
    //  swallows PHARMA, and the split lands one letter off.
    .sort(function(a, b){ return b.length - a.length; });

//  How far along a name to break when no token matches.  0.65 rather than 0.5
//  because a dead-centre cut leaves the tail longer than the head on odd
//  lengths and reads worse — POLICYBZR became POLIC/YBZR at 0.5.
const SPLIT_AT = 0.65;

function splitSymbol(sym){
  const m = sym.match(/^(.+?)[-&_](.+)$/);          // BAJAJ-AUTO, M&M, GVT&D
  if(m) return [m[1], m[2]];
  for(let i = 0; i < SUFFIX.length; i++){
    const s = SUFFIX[i];
    if(sym.length > s.length + 2 &&
       sym.slice(-s.length) === s) return [sym.slice(0, -s.length), s];
  }
  const n = Math.max(1, Math.min(sym.length - 1,
                                 Math.round(sym.length * SPLIT_AT)));
  return [sym.slice(0, n), sym.slice(n)];
}

function fitText(el, r, w, h){
  const sEl = el.querySelector('.s'), s2El = el.querySelector('.s2'),
        pEl = el.querySelector('.p');
  const sym = (r && r.symbol) ? r.symbol : '';
  const pctLen = 7;                       // '-10.58%'

  const aw = Math.max(w - 5, 4), ah = Math.max(h - 3, 4);

  const parts = splitSymbol(sym);
  const longest = Math.max(parts[0].length, parts[1].length, 1);

  //  Size the symbol on ONE line and on TWO, and keep whichever is bigger.
  //  Two lines cost vertical room but let each line be far wider, which on a
  //  narrow tile is the difference between readable and clipped.
  const rows1 = 2;
  const rows2 = 3;
  const f1 = Math.min(MAX_FS, ah / (rows1 * 1.28),
                      aw / Math.max(sym.length, 1) / CHAR_W);
  const f2 = Math.min(MAX_FS, ah / (rows2 * 1.22), aw / longest / CHAR_W);
  //  Only wrap when it is a clear win — a marginal gain is not worth the
  //  second line.
  const two = f2 > f1 * 1.12;
  //  fs is the size the name COULD take; fsym is what it is actually drawn at.
  //  Everything downstream — the readability tests included — uses the drawn
  //  size, because "is this legible" is a question about pixels on screen, not
  //  about the size the fitter would have allowed.
  let fs = two ? f2 : f1;
  let fsym = fs * SYM_SCALE;
  let fp = Math.min(fsym, aw / pctLen / CHAR_W_NUM);

  const showS = fsym >= MIN_FS && ah > 12;
  const showP = showS && fp >= MIN_FS && ah > (two ? 32 : 22);
  //  When only the name fits, it gets the whole tile height.
  if(showS && !showP){
    fsym = Math.min(MAX_FS, ah / (two ? 2.3 : 1.35),
                    aw / (two ? longest : Math.max(sym.length, 1)) / CHAR_W)
           * SYM_SCALE;
  }

  sEl.textContent = two ? parts[0] : sym;
  s2El.textContent = two ? parts[1] : '';
  sEl.style.display = showS ? '' : 'none';
  s2El.style.display = (showS && two) ? '' : 'none';
  pEl.style.display = showP ? '' : 'none';
  if(showS){
    const px = fsym.toFixed(1) + 'px';
    sEl.style.fontSize = px;
    s2El.style.fontSize = px;
  }
  if(showP) pEl.style.fontSize = fp.toFixed(1) + 'px';
}

function paint(el, r, darkKey){
  const c = C[darkKey || bandOf(r.pct)];
  if(el._bg !== c.bg){
    el.style.background = c.bg; el.style.color = c.fg; el._bg = c.bg;
  }
  const pct = (r.pct === null || r.pct === undefined)
      ? '—' : (r.pct >= 0 ? '+' : '') + r.pct.toFixed(2) + '%';
  //  The symbol text belongs to fitText — it decides one line or two, so
  //  writing r.symbol here would undo the split on every update.
  if(el._pct !== pct){ el.querySelector('.p').textContent = pct; el._pct = pct; }

  el.title = r.symbol + '  ' + pct + '\n' +
     'LTP ' + (r.ltp==null?'—':r.ltp) + '   prev ' + (r.prev_close==null?'—':r.prev_close) +
     '\nO ' + (r.open==null?'—':r.open) + '  H ' + (r.high==null?'—':r.high) +
     '  L ' + (r.low==null?'—':r.low) +
     (r.sector ? '\n' + r.sector : '') +
     '\nclick → 5-min TradingView chart';
}

function schedule(){
  if(pending) return;
  pending = true;
  requestAnimationFrame(function(){ pending = false; render(); });
}

/* ── Sector dropdown ──────────────────────────────────────────────────────── */
const secSeen = {};
function noteSector(s){
  if(!s || s === '—' || secSeen[s]) return;
  secSeen[s] = 1;
  const sel = document.getElementById('sec');
  const cur = sel.value;
  const opts = Object.keys(secSeen).sort();
  while(sel.options.length > 1) sel.remove(1);
  opts.forEach(function(v){
    const o = document.createElement('option'); o.value = v; o.textContent = v;
    sel.appendChild(o);
  });
  sel.value = cur;
}

/* ── The chart ────────────────────────────────────────────────────────────
   A tile opens tradingview.com in a new tab, on the 5-minute chart.

   The embeddable tv.js widget was tried first and dropped: TradingView does not
   serve NSE symbols through the free embed.  Checked on 2026-08-10 against
   RELIANCE, M&M and POWERINDIA — every one rendered the chart shell and then
   "This symbol is only available on TradingView", because NSE data needs a
   signed-in TradingView session that an embedded iframe on localhost does not
   carry.  The real site does have that session, so this is both simpler and
   the version that actually shows a chart — and it leaves the page with no
   external script at all, which is why it still works on the LAN with no
   internet.

   NSE tickers on TradingView replace '-' and '&' with '_':
       BAJAJ-AUTO -> NSE:BAJAJ_AUTO      M&M -> NSE:M_M      GVT&D -> NSE:GVT_D */
function tvSymbol(sym){ return 'NSE:' + String(sym||'').replace(/[-&]/g, '_'); }

/* ── Two-screen mode ──────────────────────────────────────────────────────
   Heatmap ek screen pe, chart doosri pe — ek hi Chrome, har chart naye tab me.

   The SERVER does this, not the browser, and here is why: pass `window.open`
   any feature at all (left/top/width) and Chrome makes a POPUP, and a popup has
   no tab strip. So "a positioned window that new tabs open into" cannot be
   built from JavaScript. chartwin.py launches Chrome directly with its own
   --user-data-dir, which is what makes every later call open a NEW TAB in that
   same window. The monitor position comes from Windows, not from a guess. */
let screenInfo = null;

function note(txt, tip){
  const el = document.getElementById('scrn');
  el.textContent = txt;
  document.getElementById('twoscr-lbl').title = tip || '';
}

//  Where THIS browser window sits on the virtual desktop.  screenX/screenY are
//  the only thing a page knows about its own place in the world, and the server
//  turns them into "which monitor is free" and "which Chrome profile is this".
function whereAmI(){
  return 'sx=' + Math.round(window.screenX + window.outerWidth / 2) +
         '&sy=' + Math.round(window.screenY + 40);
}

/*  Who opens the chart.

    The server can only launch Chrome on ITS OWN desktop. When the page is open
    from another PC, there is exactly one way to open a chart there — a small
    file (`chart_agent.py`) running on that PC, listening on 127.0.0.1. The page
    talks to it; no web page can touch another computer's desktop, and that is
    one of the browser's most basic security lines.

    Hence three routes, in this order:
      1. page served from localhost -> the server opens it (/api/chart)
      2. else a local agent running -> the agent opens it
      3. else                       -> an ordinary new tab                      */
const AGENT = 'http://127.0.0.1:__AGENTPORT__';
const isLocalPage = ['localhost', '127.0.0.1', '::1'].indexOf(location.hostname) >= 0;
let useAgent = false;

function enableTwoScreen(){
  const url = isLocalPage ? ('/api/screens?' + whereAmI())
                          : (AGENT + '/ping?' + whereAmI());
  fetch(url).then(function(r){ return r.json(); })
   .then(function(d){
    screenInfo = d;
    useAgent = !!d.agent;
    if(!isLocalPage && !d.agent){ throw new Error('no agent'); }
    if(isLocalPage && !d.local){
      note('(remote — unavailable)',
           'This page is open from another device and no agent is running on '
           + 'this PC. Run `python chart_agent.py`.');
      return;
    }
    if(!d.chrome){
      note('(Chrome not found)', 'Set CHROME_PATH in config.py.');
      return;
    }
    if(!d.target){
      note('(sirf ' + d.count + ' monitor)',
           'Windows only reports one display. The second monitor has to be in '
           + '"Extend" mode (Duplicate counts as one).');
      return;
    }
    const t = d.target;
    const p = (d.profile && d.profile.length)
        ? '  profile: ' + d.profile.join(' ') : '  profile: default';
    note('(2nd screen ✓)',
         'Charts open here: ' + t.width + '×' + t.height +
         ' @ x=' + t.left + ' y=' + t.top + p +
         '  —  each chart a new tab, all in one Chrome window.');
  }).catch(function(){
    if(isLocalPage){
      note('(check failed)', 'No screen info came back from the server.');
    } else {
      note('(run the agent)',
           'No chart agent is running on this PC. Copy three files from the '
           + 'HEATMAP folder (chart_agent.py, chartwin.py, config.py) here and '
           + 'run `python chart_agent.py` — charts will then open on this '
           + 'PC\'s second screen. Until then they open in a normal tab.');
    }
  });
}

function openChart(r){
  const url = 'https://www.tradingview.com/chart/?symbol=' +
              encodeURIComponent(tvSymbol(r.symbol)) + '&interval=5';
  if(!document.getElementById('twoscr').checked){
    window.open(url, '_blank', 'noopener');
    return;
  }
  //  Chrome is driven by a PROCESS, not by this page: window.open with any
  //  feature makes a popup, and popups have no tab strip.  Which process
  //  depends on where the page is being viewed — see the note above.
  const base = (isLocalPage && !useAgent) ? '/api/chart' : (AGENT + '/chart');
  fetch(base + '?symbol=' + encodeURIComponent(r.symbol) + '&' + whereAmI())
    .then(function(res){ return res.json(); })
    .then(function(d){
      if(d.ok){ note('(2nd screen ✓)', d.msg); return; }
      //  One monitor, Chrome missing, or the wrong machine — say why, and
      //  still show the chart the ordinary way rather than doing nothing.
      note('(' + (d.remote ? 'remote' : 'failed') + ')', d.msg || '');
      window.open(url, '_blank', 'noopener');
    })
    .catch(function(){ window.open(url, '_blank', 'noopener'); });
}

document.getElementById('map').addEventListener('click', function(ev){
  const t = ev.target.closest('.tile');
  if(t && rows[t.dataset.sid]) openChart(rows[t.dataset.sid]);
});

/* ── Wiring ───────────────────────────────────────────────────────────────── */
['q','sec','size','group'].forEach(function(id){
  const el = document.getElementById(id);
  el.addEventListener(id === 'q' ? 'input' : 'change', function(){
    needLayout = true; schedule();
  });
});
window.addEventListener('resize', function(){ needLayout = true; schedule(); });

//  Two-screen mode is remembered, but the PERMISSION is re-requested on every
//  load — Chrome scopes it to the page session, and asking here (rather than
//  on the first click) is what keeps the click handler synchronous.
(function initTwoScreen(){
  const cb = document.getElementById('twoscr');
  cb.checked = localStorage.getItem('heatmap.twoscr') === '1';
  if(cb.checked) enableTwoScreen();
  cb.addEventListener('change', function(){
    localStorage.setItem('heatmap.twoscr', cb.checked ? '1' : '0');
    if(cb.checked) enableTwoScreen();
    else note('', '');
  });
})();

function setStatus(t, live){
  document.getElementById('status').textContent = t;
  document.getElementById('dot').className = 'dot' + (live ? ' live' : '');
}

function ingest(list){
  list.forEach(function(r){
    const old = rows[r.sid];
    rows[r.sid] = r;
    noteSector(r.sector);
    //  A price that actually moved gets a brief flash, so the eye can find the
    //  action on a map where most tiles are still.
    if(old && old.ltp !== r.ltp && tiles[r.sid]){
      const el = tiles[r.sid];
      el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
    }
    if(!old) needLayout = true;
  });
  schedule();
}

const es = new EventSource('/api/stream');
es.onmessage = function(ev){
  const d = JSON.parse(ev.data);
  if(d.type === 'status'){ setStatus(d.text, d.live); return; }
  if(d.type === 'snapshot'){
    ingest(d.rows || []);
    setTimeout(function(){ ready = true; }, 100);
    return;
  }
  if(d.type === 'update'){ ingest(d.rows || []); }
};
es.onerror = function(){ setStatus('Reconnecting...', false); };
</script></body></html>"""
