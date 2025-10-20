# web/app.py
import os, sys, threading, logging, inspect
from collections import deque
from flask import Flask, render_template, redirect, request, url_for, flash, jsonify
from zoneinfo import ZoneInfo

# ensure project root is on path when running via `python web/app.py`
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from settings import load_config, TOKENS_PATH
from core.broker import Broker
from core.instruments import ensure_cache, build_maps
from core.stream import Stream, TickBus

# ---- strategy imports ----
from strategies.orderflow_liquidity_trap import OrderFlowLiquidityTrap
from strategies.oflt_config import CONFIG as OFLT_CONFIG

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_cfg = load_config()
_broker = Broker(
    api_key=_cfg["broker"]["api_key"],
    api_secret=_cfg["broker"]["api_secret"],
    redirect_uri=_cfg["broker"]["redirect_uri"],
    tokens_path=str(TOKENS_PATH),
)

# --- feed globals ---
_bus = TickBus()
_stream = None           # type: Stream | None
_sym2tok = {}
_tok2sym = {}
_last = {}        
_last_ts = {}           # symbol -> last_price
_last50 = {}             # symbol -> deque of last 50 ticks
MAX_TICKS = 50
_feed_lock = threading.Lock()

# --- strategy wiring ---
_strategies = []         # list of strategy instances

class StrategyContext:
    """Lightweight dependency bundle passed to strategies."""
    def __init__(self, broker, tokens_map, dry=True, exchange="NSE", tz=None, report=None):
        self.broker = broker
        self.tokens = tokens_map          # symbol -> instrument_token
        self.dry = dry
        self.exchange = exchange
        self.tz = tz or ZoneInfo("Asia/Kolkata")
        # report(symbol:str, diag:dict) -> None
        self.report = report or (lambda symbol, diag: None)

    def log(self, msg: str):
        app.logger.info(msg)

# ---- strategy diagnostics (tiny pipeline) ----
# Keep a short rolling history per symbol in memory
STRAT_DIAG = {}  # symbol -> deque(maxlen=50)
DIAG_LOCK = threading.Lock()

def report_diag(symbol: str, diag: dict):
    """Called by strategies to report one evaluation snapshot."""
    try:
        # ensure minimal fields for safety
        diag = dict(diag or {})
        diag.setdefault("symbol", symbol)
        with DIAG_LOCK:
            dq = STRAT_DIAG.get(symbol)
            if dq is None:
                dq = deque(maxlen=50)
                STRAT_DIAG[symbol] = dq
            dq.append(diag)
    except Exception:
        app.logger.exception("report_diag failed")

@app.route("/")
def home():
    access_token = _broker.load_access_token()
    status = "Connected ✅" if access_token else "Not connected"
    feed_running = _stream is not None
    with _feed_lock:
        symbols = sorted(list(_last.keys()))
        last = dict(_last)
    return render_template(
        "index.html",
        connected=bool(access_token),
        status=status,
        feed_running=feed_running,
        symbols=symbols,
        last=last
    )

@app.route("/connect")
def connect():
    api_key = _cfg["broker"]["api_key"]
    return redirect(f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}")

@app.route("/redirect")
def auth_redirect():
    req_token = request.args.get("request_token")
    if not req_token:
        flash("Missing request_token.", "danger")
        return redirect(url_for("home"))
    try:
        _broker.exchange_request_token(req_token)
        flash("Zerodha connected successfully.", "success")
    except Exception as e:
        flash(f"Login failed: {e}", "danger")
    return redirect(url_for("home"))

@app.route("/disconnect", methods=["POST"])
def disconnect():
    try:
        if os.path.exists(TOKENS_PATH):
            os.remove(TOKENS_PATH)
        flash("Disconnected. Please Connect Zerodha again.", "info")
    except Exception as e:
        flash(f"Could not clear token: {e}", "danger")
    return redirect(url_for("home"))

@app.route("/feed/start", methods=["POST"])
def feed_start():
    global _stream, _sym2tok, _tok2sym, _strategies
    if _stream is not None:
        flash("Feed already running.", "info")
        return redirect(url_for("home"))

    # must be connected
    token = _broker.load_access_token()
    if not token:
        flash("Please connect Zerodha first.", "warning")
        return redirect(url_for("home"))

    # quick sanity BEFORE starting feed
    try:
        _broker.profile()
    except Exception:
        flash("Please Connect Zerodha on this domain first (token missing/expired).", "warning")
        return redirect(url_for("home"))

    # build instruments cache & maps
    ensure_cache(_broker.kite)
    _sym2tok, _tok2sym = build_maps()

    # resolve universe → tokens
    symbols = _cfg["universe"]["symbols"]
    tokens = [_sym2tok[s] for s in symbols if s in _sym2tok]
    if not tokens:
        flash("No tokens to subscribe. Check your universe.symbols.", "danger")
        return redirect(url_for("home"))

    # UI sink: update last and last50
    def ui_sink(ticks):
        with _feed_lock:
            for t in ticks:
                sym = _tok2sym.get(t.get("instrument_token"))
                if not sym:
                    continue
                lp = t.get("last_price")
                if lp is not None:
                    _last[sym] = lp
                ts = t.get("exchange_timestamp") or t.get("timestamp") or t.get("last_trade_time")
                if ts is not None:
                    try:
                        s = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    except Exception:
                        s = str(ts)
                    _last_ts[sym] = s

                dq = _last50.get(sym)
                if dq is None:
                    dq = deque(maxlen=MAX_TICKS)
                    _last50[sym] = dq
                dq.append(dict(t))  # shallow copy

    _bus.subscribe(ui_sink)

    # ---- build & wire strategies (one per symbol) ----
    _strategies = []
    ctx = StrategyContext(
        broker=_broker,
        tokens_map=_sym2tok,
        dry=OFLT_CONFIG["dry_run"],
        exchange=OFLT_CONFIG["exchange"],
        tz=ZoneInfo("Asia/Kolkata"),
        report=report_diag,  # <<<<<< diagnostics callback
    )

    for sym in symbols:
        if sym not in _sym2tok:
            continue
        strat = OrderFlowLiquidityTrap(symbol=sym, context=ctx, overrides=None)
        _strategies.append(strat)

        # One subscription per strategy
        def make_handler(s):
            def handler(ticks):
                for t in ticks:
                    s.on_tick(t)
            return handler

        _bus.subscribe(make_handler(strat))

    try:
        _stream = Stream(api_key=_broker.api_key, access_token=token, tokens=tokens, bus=_bus, mode="FULL")
        threading.Thread(target=_stream.start, kwargs={"threaded": True}, daemon=True).start()
        flash(f"Feed started for {len(tokens)} instruments and {len(_strategies)} strategies.", "success")
    except Exception as e:
        _stream = None
        app.logger.exception("Failed to start stream")
        flash(f"Failed to start feed: {e}", "danger")

    return redirect(url_for("home"))

@app.route("/feed/stop", methods=["POST"])
def feed_stop():
    global _stream, _strategies
    if _stream:
        try:
            _stream.stop()
        except Exception:
            logging.exception("Error stopping stream")
        _stream = None
        _strategies = []
        flash("Feed stopped.", "info")
    else:
        flash("Feed is not running.", "warning")
    return redirect(url_for("home"))

@app.route("/feed/status")
def feed_status():
    running = _stream is not None
    with _feed_lock:
        payload = {"running": running, "last": dict(_last), "last_ts": dict(_last_ts)}
    return jsonify(payload)

@app.route("/feed/last50")
def feed_last50():
    symbol = request.args.get("symbol")
    if not symbol:
        return {"error": "symbol query parameter required"}, 400
    with _feed_lock:
        ticks = list(_last50.get(symbol, []))
    return {"symbol": symbol, "ticks": ticks}

# ---- NEW: strategy diagnostics endpoints ----
@app.route("/strategy/diag_all")
def strategy_diag_all():
    """Return latest snapshot per symbol (if any)."""
    out = {}
    with DIAG_LOCK:
        for sym, dq in STRAT_DIAG.items():
            if dq:
                out[sym] = dq[-1]
    return jsonify(out)

@app.route("/strategy/diag")
def strategy_diag():
    """Return last N snapshots for a given symbol."""
    symbol = request.args.get("symbol")
    n = int(request.args.get("n", 20))
    if not symbol:
        return {"error": "symbol query parameter required"}, 400
    with DIAG_LOCK:
        items = list(STRAT_DIAG.get(symbol, []))[-n:]
    return jsonify({"symbol": symbol, "items": items})

# Simple health for cloud readiness checks
@app.route("/health")
def health():
    return {"ok": True}, 200

# --- helpers ---
def _len_safe(obj):
    try:
        return len(obj)
    except Exception:
        return 0

if __name__ == "__main__":
    app.run(debug=True, port=5050)
