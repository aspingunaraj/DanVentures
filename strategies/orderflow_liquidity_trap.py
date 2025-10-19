# strategies/orderflow_liquidity_trap.py
import datetime as dt
from collections import deque
from typing import Deque, Dict, Any, List

from strategies.base import StrategyBase
from strategies.oflt_config import CONFIG


class OrderFlowLiquidityTrap(StrategyBase):
    """
    Intraday scalping: Order Flow + Momentum + Liquidity Trap.
    Works on Zerodha MODE_FULL ticks (needs depth).
    Places COVER ORDERS (CO) via ctx.broker.place_cover_order().
    """

    def __init__(self, symbol: str, context, overrides: Dict[str, Any] | None = None):
        super().__init__(symbol, context)
        self.cfg = dict(CONFIG)
        if overrides:
            self.cfg.update(overrides)

        # Shortcuts
        self.window_mom = int(self.cfg["momentum_window"])
        self.window_delta = int(self.cfg["delta_window"])
        self.window_imb = int(self.cfg["imbalance_window"])
        self.depth_levels = int(self.cfg["depth_levels"])

        # Rolling state
        self.prices: Deque[float] = deque(maxlen=self.window_mom)
        self.delta_raw: Deque[float] = deque(maxlen=self.window_delta)
        self.imbalance_raw: Deque[float] = deque(maxlen=self.window_imb)

        # For absorption detection
        self.abs_range_prices: Deque[float] = deque(maxlen=int(self.cfg["absorption_window"]))
        self.abs_qty: Deque[float] = deque(maxlen=int(self.cfg["absorption_window"]))

        # VWAP (incremental approximation using last traded qty)
        self.cum_pv = 0.0
        self.cum_v = 0.0
        self.vwap_history: Deque[float] = deque(maxlen=int(self.cfg["vwap_slope_window"]))

        # Position / gating
        self.position: str | None = None     # "LONG", "SHORT", or None
        self.last_entry_ts: float | None = None
        self.trades_taken: int = 0

    # -------------- Helpers (safe extractors) -----------------
    @staticmethod
    def _best_bid_ask_from_depth(depth: Dict[str, Any]):
        """
        Zerodha 'depth' tick shape:
          depth = {'buy': [{'price':..., 'quantity':...}, ... up to 5],
                   'sell':[{'price':..., 'quantity':...}, ... up to 5]}
        """
        buy = (depth or {}).get("buy") or []
        sell = (depth or {}).get("sell") or []
        best_bid = buy[0]["price"] if buy else None
        best_ask = sell[0]["price"] if sell else None
        return best_bid, best_ask, buy, sell

    @staticmethod
    def _sum_top_qty(levels: List[Dict[str, Any]], n: int) -> float:
        return float(sum((levels[i]["quantity"] for i in range(min(n, len(levels)))), ))

    @staticmethod
    def _safe_float(x, default=None):
        try:
            return float(x)
        except Exception:
            return default

    # -------------- Core per-tick processing -----------------
    def on_tick(self, tick: Dict[str, Any]):
        # Ensure tick is for me
        tok = self.ctx.tokens.get(self.symbol)
        if not tok or tick.get("instrument_token") != tok:
            return

        lp = self._safe_float(tick.get("last_price"))
        if lp is None:
            return

        depth = tick.get("depth") or {}
        best_bid, best_ask, buy_levels, sell_levels = self._best_bid_ask_from_depth(depth)

        # Spread & big-jump filters asap
        if best_bid and best_ask:
            mid = 0.5 * (best_bid + best_ask)
            spread_pct = (best_ask - best_bid) / mid if mid else 0.0
            if spread_pct > self.cfg["max_spread_pct"]:
                if self.cfg["log_rejections"]:
                    self.ctx.log(f"[{self.symbol}] Reject: spread too wide {spread_pct:.4%}")
                return

            # Big single-tick jump filter
            if self.prices:
                jump_bps = abs(lp - self.prices[-1]) / self.prices[-1] * 10000
                if jump_bps > self.cfg["max_tick_jump_bps"]:
                    if self.cfg["log_rejections"]:
                        self.ctx.log(f"[{self.symbol}] Reject: tick jump {jump_bps:.2f} bps too large")
                    return

        # Update rolling state
        self.prices.append(lp)

        # --- Trade direction delta ---
        # Classify tick by comparing LTP to top-of-book; approximate traded qty:
        # Zerodha tick may carry 'last_traded_quantity' or 'last_quantity' (varies by version); fallback to 1
        ltq = tick.get("last_traded_quantity") or tick.get("last_quantity") or 1
        ltq = self._safe_float(ltq, 1.0)

        buy_init = sell_init = 0.0
        if best_ask and lp >= best_ask:
            buy_init = ltq
        elif best_bid and lp <= best_bid:
            sell_init = ltq
        # else unclassified (mid prints)

        delta_tick = buy_init - sell_init
        self.delta_raw.append(delta_tick)

        # --- Bid-ask imbalance (using top-of-book quantities) ---
        bid_qty = self._sum_top_qty(buy_levels, self.depth_levels)
        ask_qty = self._sum_top_qty(sell_levels, self.depth_levels)
        denom = (bid_qty + ask_qty) or 1.0
        imbalance_tick = (bid_qty - ask_qty) / denom
        self.imbalance_raw.append(imbalance_tick)

        # --- Absorption tracking ---
        self.abs_range_prices.append(lp)
        self.abs_qty.append(ltq)

        # --- VWAP update (approximate per-tick) ---
        self.cum_pv += lp * ltq
        self.cum_v += ltq
        vwap = (self.cum_pv / self.cum_v) if self.cum_v > 0 else lp
        self.vwap_history.append(vwap)

        # Evaluate only when we have enough data
        if len(self.prices) < max(5, self.window_mom) or \
           len(self.delta_raw) < self.window_delta or \
           len(self.imbalance_raw) < self.window_imb:
            return

        # ------------------- Filters -------------------
        if not self._session_filters_pass():
            return

        if self.cfg["use_vwap_filter"]:
            if not self._vwap_filter_pass(lp):
                return

        # ------------------- Signals -------------------
        long_ok, short_ok, reasons = self._compute_signals(lp, best_bid, best_ask, bid_qty, ask_qty)

        if self.cfg["log_signals"]:
            self.ctx.log(f"[{self.symbol}] sig long_ok={long_ok} short_ok={short_ok} :: {', '.join(reasons)}")

        # Cooldown / max trades per session
        now_ts = dt.datetime.now(self.ctx.tz).timestamp()
        if self.last_entry_ts and (now_ts - self.last_entry_ts) < self.cfg["cooldown_seconds"]:
            return
        if self.trades_taken >= self.cfg["max_trades_per_session"]:
            return

        # Already in a position? (We’re demonstrating single-side-at-a-time)
        if self.position is not None:
            return

        # Enter
        if long_ok:
            self._enter(side="BUY", price=lp)
        elif short_ok:
            self._enter(side="SELL", price=lp)

    # ------------------- Filters implementations -------------------
    def _session_filters_pass(self) -> bool:
        """Time-of-day, first minutes, lunch window, best windows."""
        now = dt.datetime.now(self.ctx.tz)
        # Market assumed 09:15–15:30 IST
        mkt_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if (now - mkt_open).total_seconds() < self.cfg["skip_first_minutes"] * 60:
            return False

        def _in_range(t, s):
            h, m = map(int, s.split(":"))
            return t.hour > h or (t.hour == h and t.minute >= m)

        def _before(t, s):
            h, m = map(int, s.split(":"))
            return t.hour < h or (t.hour == h and t.minute < m)

        # Lunch skip
        if _in_range(now, self.cfg["lunch_skip_start"]) and _before(now, self.cfg["lunch_skip_end"]):
            return False

        if self.cfg["use_best_windows"]:
            ok_any = False
            for a, b in self.cfg["best_windows"]:
                if _in_range(now, a) and _before(now, b):
                    ok_any = True; break
            if not ok_any:
                return False

        return True

    def _vwap_filter_pass(self, last_price: float) -> bool:
        if not self.vwap_history:
            return False
        vwap = self.vwap_history[-1]
        # Slope: VWAP now vs oldest in slope window
        slope_ok_long = vwap > self.vwap_history[0]
        slope_ok_short = vwap < self.vwap_history[0]
        # Alignment
        above = last_price > vwap
        below = last_price < vwap
        long_pass = above and slope_ok_long
        short_pass = below and slope_ok_short
        # We don't decide direction here; just require at least one to be possible
        return long_pass or short_pass

    # ------------------- Signal computation -------------------
    def _compute_signals(self, lp, best_bid, best_ask, bid_qty, ask_qty):
        reasons = []
        # Micro momentum
        hh_hl = self._is_hh_hl(self.prices)
        ll_lh = self._is_ll_lh(self.prices)
        # Delta trend
        delta_inc = self._is_strictly_increasing(self.delta_raw, self.cfg["delta_trend_ticks"])
        delta_dec = self._is_strictly_decreasing(self.delta_raw, self.cfg["delta_trend_ticks"])
        # Imbalance averages
        imb_avg = sum(self.imbalance_raw) / len(self.imbalance_raw)
        # Depth pressure
        depth_ratio = (bid_qty / max(1.0, ask_qty)) if (bid_qty and ask_qty) else 1.0

        # Absorption (optional but powerful)
        absorp_ok_long, absorp_ok_short = self._absorption_ok()

        # VWAP direction alignment
        vwap = self.vwap_history[-1] if self.vwap_history else lp
        vwap_slope_up = self.vwap_history[-1] > self.vwap_history[0] if len(self.vwap_history) >= 2 else True
        vwap_slope_dn = self.vwap_history[-1] < self.vwap_history[0] if len(self.vwap_history) >= 2 else True

        long_ok = (
            (not self.cfg["require_hh_hl_for_long"] or hh_hl) and
            delta_inc and
            (imb_avg >= self.cfg["imbalance_threshold_long"]) and
            (depth_ratio >= self.cfg["depth_ratio_min_long"]) and
            (not self.cfg["use_vwap_filter"] or (lp > vwap and vwap_slope_up)) and
            (absorp_ok_long or True)  # keep absorption optional for v1; set to 'and absorp_ok_long' to enforce
        )

        short_ok = (
            (not self.cfg["require_ll_lh_for_short"] or ll_lh) and
            delta_dec and
            (imb_avg <= self.cfg["imbalance_threshold_short"]) and
            (depth_ratio <= self.cfg["depth_ratio_max_short"]) and
            (not self.cfg["use_vwap_filter"] or (lp < vwap and vwap_slope_dn)) and
            (absorp_ok_short or True)
        )

        reasons.append(f"imb_avg={imb_avg:.2f} depth_ratio={depth_ratio:.2f} "
                       f"delta_inc={delta_inc} delta_dec={delta_dec} "
                       f"hh_hl={hh_hl} ll_lh={ll_lh} vwap={vwap:.2f}")

        return long_ok, short_ok, reasons

    # ------------------- Pattern helpers -------------------
    @staticmethod
    def _is_hh_hl(prices: Deque[float]) -> bool:
        if len(prices) < 4:
            return False
        p = list(prices)[-4:]
        return (p[1] > p[0]) and (p[2] > p[1]) and (p[3] > p[2]) and (min(p[1], p[2], p[3]) > p[0])

    @staticmethod
    def _is_ll_lh(prices: Deque[float]) -> bool:
        if len(prices) < 4:
            return False
        p = list(prices)[-4:]
        return (p[1] < p[0]) and (p[2] < p[1]) and (p[3] < p[2]) and (max(p[1], p[2], p[3]) < p[0])

    @staticmethod
    def _is_strictly_increasing(seq: Deque[float], k: int) -> bool:
        if len(seq) < k:
            return False
        s = list(seq)[-k:]
        return all(s[i] < s[i+1] for i in range(len(s)-1))

    @staticmethod
    def _is_strictly_decreasing(seq: Deque[float], k: int) -> bool:
        if len(seq) < k:
            return False
        s = list(seq)[-k:]
        return all(s[i] > s[i+1] for i in range(len(s)-1))

    def _absorption_ok(self):
        """Detects 'lots of trading, little movement' over the absorption window."""
        if len(self.abs_range_prices) < max(3, int(self.cfg["absorption_window"])):
            return (False, False)
        rng = max(self.abs_range_prices) - min(self.abs_range_prices)
        # convert to basis points relative to mid of window
        mid = 0.5 * (max(self.abs_range_prices) + min(self.abs_range_prices))
        rng_bps = (rng / mid * 10000) if mid else 0.0
        traded = sum(self.abs_qty)
        ok = (traded >= self.cfg["absorption_min_traded_qty"]) and \
             (rng_bps <= self.cfg["absorption_max_price_range_bps"])
        # For now, allow absorption to support both sides (context will use other conditions to choose)
        return (ok, ok)

    # ------------------- Execution -------------------
    def _enter(self, side: str, price: float):
        # Basic gating: dry run, then place CO with trigger derived from stoploss_pct
        sl_pct = float(self.cfg["stoploss_pct"])
        if side == "BUY":
            sl_trig = round(price * (1.0 - sl_pct), 2)
        else:
            sl_trig = round(price * (1.0 + sl_pct), 2)

        qty = int(self.cfg["qty"])
        exch = self.cfg["exchange"]
        dry = bool(self.cfg["dry_run"])

        self.ctx.log(f"[{self.symbol}] ENTER {side}-CO qty={qty} entry≈{price:.2f} sl_trig={sl_trig:.2f} dry={dry}")

        if dry:
            # Simulate fill
            self.position = "LONG" if side == "BUY" else "SHORT"
            self.last_entry_ts = dt.datetime.now(self.ctx.tz).timestamp()
            self.trades_taken += 1
            return

        try:
            resp = self.ctx.broker.place_cover_order(
                exchange=exch,
                symbol=self.symbol,
                side=side,
                qty=qty,
                trigger_price=sl_trig,
                order_type="MARKET",
            )
            self.ctx.log(f"[{self.symbol}] {side}-CO placed: {resp}")
            self.position = "LONG" if side == "BUY" else "SHORT"
            self.last_entry_ts = dt.datetime.now(self.ctx.tz).timestamp()
            self.trades_taken += 1
        except Exception as e:
            self.ctx.log(f"[{self.symbol}] {side}-CO FAILED: {e}")
