# web/app.py
import os, sys, threading, logging
import json
from collections import deque
from flask import Flask, render_template, redirect, request, url_for, flash, jsonify
from zoneinfo import ZoneInfo
from pathlib import Path
from copy import deepcopy

# ensure project root is on path when running via `python web/app.py`
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from settings import load_config, TOKENS_PATH
from core.broker import Broker
from core.instruments import ensure_cache, build_maps
from core.stream import Stream, TickBus

# ---- strategy imports ----
from strategies.orderflow_liquidity_trap import OrderFlowLiquidityTrap
from strategies.oflt_config import CONFIG as BASE_OFLT_CONFIG

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
_last = {}              # symbol -> last_price
_last_ts = {}           # symbol -> last tick timestamp (string)
_last50 = {}            # symbol -> deque of last 50 ticks
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
STRAT_DIAG = {}  # symbol -> deque(maxlen=50)
DIAG_LOCK = threading.Lock()

def report_diag(symbol: str, diag: dict):
    """Called by strategies to report one evaluation snapshot."""
    try:
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

# ---- config overrides (persisted) ----
_OVERRIDES_PATH = Path(os.path.dirname(os.path.dirname(__file__))) / "oflt_overrides.json"
_config_overrides = {}

# Changes to these keys require strategy rebuild (Stop Feed → Start Feed)
_STRUCTURAL_KEYS = {
    "momentum_window",
    "delta_window",
    "imbalance_window",
    "depth_levels",
    "vwap_slope_window",
    "absorption_window",
}

def _load_overrides():
    """Load persisted overrides into memory."""
    global _config_overrides
    if _OVERRIDES_PATH.exists():
        try:
            _config_overrides = json.loads(_OVERRIDES_PATH.read_text())
        except Exception:
            app.logger.exception("Failed reading overrides file; starting with empty overrides.")
            _config_overrides = {}
    else:
        _config_overrides = {}

def _save_overrides():
    """Persist current overrides to disk."""
    try:
        _OVERRIDES_PATH.write_text(json.dumps(_config_overrides, indent=2, sort_keys=True))
    except Exception:
        app.logger.exception("Failed writing overrides file.")

def _effective_config():
    """Base CONFIG + overrides."""
    eff = deepcopy(BASE_OFLT_CONFIG)
    eff.update(_config_overrides or {})
    return eff

def _coerce_types(base_cfg: dict, raw: dict) -> dict:
    """
    Convert incoming JSON/form strings to correct types using the base config as a guide.
    Only known keys are returned.
    """
    out = {}
    for k, v in (raw or {}).items():
        if k not in base_cfg:
            continue
        base_val = base_cfg[k]
        t = type(base_val)
        try:
            if t is bool:
                if isinstance(v, bool):
                    out[k] = v
                else:
                    out[k] = str(v).lower() in ("1", "true", "yes", "on")
            elif t is int:
                out[k] = int(v)
            elif t is float:
                out[k] = float(v)
            elif isinstance(base_val, (list, tuple)):
                # expect array in JSON; for form posts it's trickier; keep as-is
                out[k] = v
            else:
                out[k] = v
        except Exception:
            # ignore bad casts; skip key
            pass
    return out

# Load overrides now
_load_overrides()

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

    # UI sink: update last & timestamp & last50
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
    eff_cfg = _effective_config()
    ctx = StrategyContext(
        broker=_broker,
        tokens_map=_sym2tok,
        dry=eff_cfg.get("dry_run", True),
        exchange=eff_cfg.get("exchange", "NSE"),
        tz=ZoneInfo("Asia/Kolkata"),
        report=report_diag,  # diagnostics callback
    )

    for sym in symbols:
        if sym not in _sym2tok:
            continue
        # Pass ONLY overrides to allow strategy to merge with its base CONFIG
        strat = OrderFlowLiquidityTrap(symbol=sym, context=ctx, overrides=deepcopy(_config_overrides))
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
        payload = {
            "running": running,
            "last": dict(_last),
            "last_ts": dict(_last_ts)
        }
    return jsonify(payload)

@app.route("/feed/last50")
def feed_last50():
    symbol = request.args.get("symbol")
    if not symbol:
        return {"error": "symbol query parameter required"}, 400
    with _feed_lock:
        ticks = list(_last50.get(symbol, []))
    return {"symbol": symbol, "ticks": ticks}

# ---- strategy diagnostics endpoints ----
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

# ---- Strategy Config (GET current, POST save overrides, RESET) ----
@app.route("/strategy/config", methods=["GET"])
def strategy_config_get():
    base = deepcopy(BASE_OFLT_CONFIG)
    eff = _effective_config()
    return jsonify({
        "base": base,
        "overrides": deepcopy(_config_overrides),
        "effective": eff,
    })

@app.route("/strategy/config", methods=["POST"])
def strategy_config_post():
    # Accept JSON body or form-encoded
    incoming = request.get_json(silent=True) or dict(request.form)
    base = deepcopy(BASE_OFLT_CONFIG)
    old_eff = _effective_config()

    # Coerce to proper types guided by base
    new_overrides = _coerce_types(base, incoming)

    # Predict if structural change occurs
    future_overrides = deepcopy(_config_overrides)
    future_overrides.update(new_overrides)
    new_eff = deepcopy(base); new_eff.update(future_overrides)

    rebuild_required = any(
        (str(old_eff.get(k)) != str(new_eff.get(k)))
        for k in _STRUCTURAL_KEYS
    )

    # Persist overrides
    _config_overrides.update(new_overrides)
    _save_overrides()

    # Live-apply non-structural changes to running strategies
    applied_live = False
    if _strategies and not rebuild_required:
        for s in _strategies:
            try:
                s.cfg.update(new_overrides)
                applied_live = True
            except Exception:
                app.logger.exception("Failed applying overrides to a live strategy")

    return jsonify({
        "ok": True,
        "rebuild_required": rebuild_required,
        "applied_live": applied_live,
        "effective": new_eff,
        "note": ("Window-size changes require Stop Feed → Start Feed to take effect."
                 if rebuild_required else
                 "Non-structural fields applied to running strategies."),
    })

@app.route("/strategy/config/reset", methods=["POST"])
def strategy_config_reset():
    global _config_overrides
    _config_overrides = {}
    _save_overrides()
    return jsonify({"ok": True, "message": "Overrides cleared. Using base config now."})

# --- Square off ALL active strategy positions ---
@app.route("/squareoff", methods=["POST"])
def square_off_all():
    global _strategies
    if not _strategies:
        flash("No strategies are active.", "warning")
        return redirect(url_for("home"))

    exited, skipped, errors = [], [], []
    for s in _strategies:
        sym = getattr(s, "symbol", "?")
        pos = getattr(s, "position", None)
        if not pos:
            skipped.append(sym)
            continue

        dry = bool(getattr(s, "cfg", {}).get("dry_run", True))
        parent_id = getattr(s, "parent_order_id", None)

        try:
            if dry:
                # simulate exit
                s.position = None
                s.entry_price = None
                s.parent_order_id = None
                exited.append(f"{sym} (dry)")
            else:
                if not parent_id:
                    errors.append(f"{sym}: no parent_order_id (cannot exit CO)")
                else:
                    _broker.exit_cover_order(parent_order_id=str(parent_id))
                    exited.append(sym)
                # clear local state regardless
                s.position = None
                s.entry_price = None
                s.parent_order_id = None
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if exited:
        flash(f"Square off sent for: {', '.join(exited)}", "success")
    if skipped:
        flash(f"No open position for: {', '.join(skipped)}", "info")
    if errors:
        flash(f"Errors: {', '.join(errors)}", "danger")

    return redirect(url_for("home"))


# --- OPTIONAL: Square off ONE symbol ---
@app.route("/squareoff/<symbol>", methods=["POST"])
def square_off_symbol(symbol):
    global _strategies
    target = None
    for s in _strategies:
        if getattr(s, "symbol", "").upper() == symbol.upper():
            target = s
            break
    if not target:
        flash(f"No strategy found for {symbol}.", "warning")
        return redirect(url_for("home"))

    if not getattr(target, "position", None):
        flash(f"{symbol} is already flat.", "info")
        return redirect(url_for("home"))

    dry = bool(getattr(target, "cfg", {}).get("dry_run", True))
    pid = getattr(target, "parent_order_id", None)

    try:
        if dry:
            target.position = None
            target.entry_price = None
            target.parent_order_id = None
            flash(f"{symbol}: (dry) squared off.", "success")
        else:
            if not pid:
                flash(f"{symbol}: no parent_order_id to exit CO.", "danger")
            else:
                _broker.exit_cover_order(parent_order_id=str(pid))
                flash(f"{symbol}: square off sent.", "success")
            # clear local
            target.position = None
            target.entry_price = None
            target.parent_order_id = None
    except Exception as e:
        flash(f"{symbol}: square off failed: {e}", "danger")

    return redirect(url_for("home"))





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
