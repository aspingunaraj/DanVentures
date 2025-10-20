# strategies/orderflow_liquidity_trap.py
import datetime as dt
from collections import deque
from typing import Deque, Dict, Any, List

from strategies.base import StrategyBase
from strategies.oflt_config import CONFIG



class OrderFlowLiquidityTrap(StrategyBase):
    """
    Intraday scalping: Order Flow + Momentum + Liquidity Trap.
    Needs MODE_FULL ticks (depth).
    Enters with CO; exits by exiting the CO child (via Broker helper).
    Emits per-evaluation diagnostics via self.ctx.report(symbol, diag).
    """

    def __init__(self, symbol: str, context, overrides: Dict[str, Any] | None = None):
        super().__init__(symbol, context)
        self.cfg = dict(CONFIG)
        if overrides:
            self.cfg.update(overrides)

        # Windows
        self.window_mom = int(self.cfg["momentum_window"])
        self.window_delta = int(self.cfg["delta_window"])
        self.window_imb = int(self.cfg["imbalance_window"])
        self.depth_levels = int(self.cfg["depth_levels"])

        # Rolling state
        self.prices: Deque[float] = deque(maxlen=self.window_mom)
        self.delta_raw: Deque[float] = deque(maxlen=self.window_delta)
        self.imbalance_raw: Deque[float] = deque(maxlen=self.window_imb)

        # Absorption tracking
        self.abs_range_prices: Deque[float] = deque(maxlen=int(self.cfg["absorption_window"]))
        self.abs_qty: Deque[float] = deque(maxlen=int(self.cfg["absorption_window"]))

        # VWAP
        self.cum_pv = 0.0
        self.cum_v = 0.0
        self.vwap_history: Deque[float] = deque(maxlen=int(self.cfg["vwap_slope_window"]))

        # Position / trade state
        self.position: str | None = None           # "LONG"/"SHORT"/None
        self.entry_price: float | None = None
        self.parent_order_id: str | None = None    # CO parent id (needed for exit)
        self.last_entry_ts: float | None = None
        self.trades_taken: int = 0

    # ------------------- Tick processing -------------------
    def round_to_tick(value: float, tick: float=0.10) -> float:
        return float(f"{round(value / tick) * tick:.2f}")


    def on_tick(self, tick: Dict[str, Any]):
        tok = self.ctx.tokens.get(self.symbol)
        if not tok or tick.get("instrument_token") != tok:
            return

        lp = self._safe_float(tick.get("last_price"))
        if lp is None:
            return

        depth = tick.get("depth") or {}
        best_bid, best_ask, buy_levels, sell_levels = self._best_depth(depth)
        now_dt = dt.datetime.now(self.ctx.tz)

        # Spread & jump filter
        spread_ok = True
        jump_ok = True
        if best_bid and best_ask:
            mid = 0.5 * (best_bid + best_ask)
            spread_pct = (best_ask - best_bid) / mid if mid else 0.0
            if spread_pct > self.cfg["max_spread_pct"]:
                spread_ok = False
                if self.cfg["log_rejections"]:
                    self.ctx.log(f"[{self.symbol}] Reject: spread too wide {spread_pct:.4%}")
                # Emit a diagnostic showing why we skipped
                self._emit_diag(
                    when_ts=now_dt, lp=lp, vwap=(self.vwap_history[-1] if self.vwap_history else lp),
                    spread_ok=False, jump_ok=True,
                    session_ok=None, vwap_ok=None,
                    imb_avg=None, depth_ratio=None, delta_slope="flat", momentum_code="-",
                    absorption_ok=False, long_ok=False, short_ok=False,
                    decision_action="HOLD", position_state=(self.position or "FLAT"),
                )
                # If we ARE IN a position and spread widens dangerously -> exit for safety
                if self.position and self._should_exit_on_spread(spread_pct):
                    self._exit(reason=f"spread {spread_pct:.4%} > limit")
                return

            # Big single-tick jump
            if self.prices:
                jump_bps = abs(lp - self.prices[-1]) / self.prices[-1] * 10000
                if jump_bps > self.cfg["max_tick_jump_bps"]:
                    jump_ok = False
                    if self.cfg["log_rejections"]:
                        self.ctx.log(f"[{self.symbol}] Reject: tick jump {jump_bps:.2f} bps too large")
                    self._emit_diag(
                        when_ts=now_dt, lp=lp, vwap=(self.vwap_history[-1] if self.vwap_history else lp),
                        spread_ok=True, jump_ok=False,
                        session_ok=None, vwap_ok=None,
                        imb_avg=None, depth_ratio=None, delta_slope="flat", momentum_code="-",
                        absorption_ok=False, long_ok=False, short_ok=False,
                        decision_action="HOLD", position_state=(self.position or "FLAT"),
                    )
                    # Also consider safety exit on extreme volatility
                    if self.position and jump_bps > (self.cfg["max_tick_jump_bps"] * 2):
                        self._exit(reason=f"vol spike {jump_bps:.1f}bps")
                    return

        # Rolling updates
        self.prices.append(lp)
        ltq = tick.get("last_traded_quantity") or tick.get("last_quantity") or 1
        ltq = self._safe_float(ltq, 1.0)

        # Delta classification
        buy_init, sell_init = 0.0, 0.0
        if best_ask and lp >= best_ask:
            buy_init = ltq
        elif best_bid and lp <= best_bid:
            sell_init = ltq
        self.delta_raw.append(buy_init - sell_init)

        # Imbalance
        bid_qty = self._sum_qty(buy_levels, self.depth_levels)
        ask_qty = self._sum_qty(sell_levels, self.depth_levels)
        denom = (bid_qty + ask_qty) or 1.0
        self.imbalance_raw.append((bid_qty - ask_qty) / denom)

        # Absorption windows
        self.abs_range_prices.append(lp)
        self.abs_qty.append(ltq)

        # VWAP
        self.cum_pv += lp * ltq
        self.cum_v += ltq
        vwap = (self.cum_pv / self.cum_v) if self.cum_v > 0 else lp
        self.vwap_history.append(vwap)

        # Need minimum data
        if len(self.prices) < max(5, self.window_mom) or \
           len(self.delta_raw) < self.window_delta or \
           len(self.imbalance_raw) < self.window_imb:
            return

        # If IN position -> check exits first (target, time stop, opposite flow)
        if self.position:
            if self._should_exit_now(lp, bid_qty, ask_qty):
                # Emit EXIT decision snapshot before exiting
                self._emit_diag(
                    when_ts=now_dt, lp=lp, vwap=vwap,
                    spread_ok=spread_ok, jump_ok=jump_ok,
                    session_ok=True, vwap_ok=True,
                    imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw) if self.imbalance_raw else None),
                    depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                    delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                    momentum_code=self._momentum_code(),
                    absorption_ok=any(self._absorption_ok()),
                    long_ok=False, short_ok=False,
                    decision_action="EXIT", position_state=(self.position or "FLAT"),
                )
                self._exit(reason="exit signal/target/time stop")
            return

        # Filters (evaluate and emit if failing)
        session_ok = self._session_filters_pass()
        if not session_ok:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=False, vwap_ok=None,
                imb_avg=None, depth_ratio=None, delta_slope="flat", momentum_code="-",
                absorption_ok=False, long_ok=False, short_ok=False,
                decision_action="HOLD", position_state=(self.position or "FLAT"),
            )
            return

        vwap_ok = (not self.cfg["use_vwap_filter"]) or self._vwap_filter_pass(lp)
        if self.cfg["use_vwap_filter"] and not vwap_ok:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=False,
                imb_avg=None, depth_ratio=None, delta_slope="flat", momentum_code="-",
                absorption_ok=False, long_ok=False, short_ok=False,
                decision_action="HOLD", position_state=(self.position or "FLAT"),
            )
            return

        # Signals
        long_ok, short_ok, reasons = self._signals(lp, bid_qty, ask_qty)
        if self.cfg["log_signals"]:
            self.ctx.log(f"[{self.symbol}] sig long={long_ok} short={short_ok} :: {', '.join(reasons)}")

        # Cooldown / trade cap
        now_ts = now_dt.timestamp()
        if self.last_entry_ts and (now_ts - self.last_entry_ts) < self.cfg["cooldown_seconds"]:
            # Emit HOLD due to cooldown
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=True,
                imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw)),
                depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                momentum_code=self._momentum_code(),
                absorption_ok=any(self._absorption_ok()),
                long_ok=long_ok, short_ok=short_ok,
                decision_action="HOLD", position_state=(self.position or "FLAT"),
            )
            return

        if self.trades_taken >= self.cfg["max_trades_per_session"]:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=True,
                imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw)),
                depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                momentum_code=self._momentum_code(),
                absorption_ok=any(self._absorption_ok()),
                long_ok=long_ok, short_ok=short_ok,
                decision_action="HOLD", position_state=(self.position or "FLAT"),
            )
            return

        # Decide + emit + act
        if long_ok:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=True,
                imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw)),
                depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                momentum_code=self._momentum_code(),
                absorption_ok=any(self._absorption_ok()),
                long_ok=True, short_ok=False,
                decision_action="ENTER_LONG", position_state=(self.position or "FLAT"),
            )
            self._enter(side="BUY", price=lp)
        elif short_ok:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=True,
                imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw)),
                depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                momentum_code=self._momentum_code(),
                absorption_ok=any(self._absorption_ok()),
                long_ok=False, short_ok=True,
                decision_action="ENTER_SHORT", position_state=(self.position or "FLAT"),
            )
            self._enter(side="SELL", price=lp)
        else:
            self._emit_diag(
                when_ts=now_dt, lp=lp, vwap=vwap,
                spread_ok=spread_ok, jump_ok=jump_ok,
                session_ok=True, vwap_ok=True,
                imb_avg=(sum(self.imbalance_raw) / len(self.imbalance_raw)),
                depth_ratio=(bid_qty / max(1.0, ask_qty) if (bid_qty and ask_qty) else None),
                delta_slope=self._delta_slope_tag(self.cfg["delta_trend_ticks"]),
                momentum_code=self._momentum_code(),
                absorption_ok=any(self._absorption_ok()),
                long_ok=False, short_ok=False,
                decision_action="HOLD", position_state=(self.position or "FLAT"),
            )

    # ------------------- Entry / Exit -------------------
    def _enter(self, side: str, price: float):
        sl_pct = float(self.cfg["stoploss_pct"])
        raw_trig = round(price * (1.0 - sl_pct), 2) if side == "BUY" else round(price * (1.0 + sl_pct), 2)
        tick_size = float(self.cfg.get("tick_size", 0.10))
        sl_trig = self.round_to_tick(raw_trig, tick_size)
        qty = int(self.cfg["qty"])
        dry = bool(self.cfg["dry_run"])

        self.ctx.log(f"[{self.symbol}] ENTER {side}-CO qty={qty} entry≈{price:.2f} sl_trig={sl_trig:.2f} dry={dry}")

        self.position = "LONG" if side == "BUY" else "SHORT"
        self.entry_price = float(price)
        self.last_entry_ts = dt.datetime.now(self.ctx.tz).timestamp()
        self.trades_taken += 1
      
        if dry:
            self.parent_order_id = "DRY_PARENT"
            return

        try:
            resp = self.ctx.broker.place_cover_order(
                exchange=self.cfg["exchange"],
                symbol=self.symbol,
                side=side,
                qty=qty,
                trigger_price=sl_trig,
                order_type="MARKET",
            )
            # For CO, place_order returns the PARENT order_id
            self.parent_order_id = str(resp.get("order_id") or "")
            self.ctx.log(f"[{self.symbol}] {side}-CO placed, parent_order_id={self.parent_order_id}")
        except Exception as e:
            self.ctx.log(f"[{self.symbol}] {side}-CO FAILED: {e}")
            # Reset state on failure
            self.position = None
            self.entry_price = None
            self.parent_order_id = None


    def _exit(self, reason: str):
        if not self.position:
            return
        dry = bool(self.cfg["dry_run"])
        self.ctx.log(f"[{self.symbol}] EXIT ({reason}) dry={dry}")

        if dry:
            # simulate
            self.position = None
            self.entry_price = None
            self.parent_order_id = None
            return

        try:
            if not self.parent_order_id:
                raise RuntimeError("no parent_order_id to exit CO")
            # Exit CO by exiting child leg (helper figures out child id)
            exited_id = self.ctx.broker.exit_cover_order(parent_order_id=self.parent_order_id)
            self.ctx.log(f"[{self.symbol}] CO exit sent, child order_id={exited_id}")
        except Exception as e:
            self.ctx.log(f"[{self.symbol}] CO exit FAILED: {e}")
            return
        finally:
            # Clear local state regardless; if needed you can poll orderbook to confirm
            self.position = None
            self.entry_price = None
            self.parent_order_id = None

    # ------------------- Exit conditions -------------------
    def _should_exit_now(self, lp: float, bid_qty: float, ask_qty: float) -> bool:
        """
        Composite exit rule:
        - Target reached
        - Opposite flow (imbalance/delta) against position
        - Time stop
        - Optional deterioration in depth pressure
        """
        # Target
        tgt_pct = float(self.cfg.get("target_pct", 0.001))  # default 0.10% if not in config
        if self.entry_price:
            ret = (lp - self.entry_price) / self.entry_price if self.position == "LONG" \
                  else (self.entry_price - lp) / self.entry_price
            if ret >= tgt_pct:
                self.ctx.log(f"[{self.symbol}] target hit {ret:.3%} >= {tgt_pct:.3%}")
                return True

        # Time stop
        ts = float(self.cfg.get("time_stop_seconds", 0)) or 0.0
        if ts > 0 and self.last_entry_ts:
            if (dt.datetime.now(self.ctx.tz).timestamp() - self.last_entry_ts) >= ts:
                self.ctx.log(f"[{self.symbol}] time stop reached {ts}s")
                return True

        # Opposite flow: imbalance & delta trend
        k = int(self.cfg["delta_trend_ticks"])
        opp_imb = self._imbalance_against_position()
        opp_delta = self._delta_trend_against_position(k)
        if opp_imb or opp_delta:
            self.ctx.log(f"[{self.symbol}] opposite flow (imbalance={opp_imb}, delta_trend={opp_delta})")
            return True

        # Depth deterioration (optional): pressure flips
        depth_ratio = (bid_qty / max(1.0, ask_qty)) if (bid_qty and ask_qty) else 1.0
        if self.position == "LONG" and depth_ratio < self.cfg["depth_ratio_max_short"]:
            self.ctx.log(f"[{self.symbol}] depth flipped against LONG (ratio={depth_ratio:.2f})")
            return True
        if self.position == "SHORT" and depth_ratio > self.cfg["depth_ratio_min_long"]:
            self.ctx.log(f"[{self.symbol}] depth flipped against SHORT (ratio={depth_ratio:.2f})")
            return True

        return False

    def _should_exit_on_spread(self, spread_pct: float) -> bool:
        # if spread explodes 2x our entry filter while in position, prefer to flatten
        return spread_pct > (self.cfg["max_spread_pct"] * 2.0)

    def _imbalance_against_position(self) -> bool:
        if not self.imbalance_raw:
            return False
        imb_avg = sum(self.imbalance_raw) / len(self.imbalance_raw)
        if self.position == "LONG":
            return imb_avg <= self.cfg["imbalance_threshold_short"]
        else:
            return imb_avg >= self.cfg["imbalance_threshold_long"]

    def _delta_trend_against_position(self, k: int) -> bool:
        if len(self.delta_raw) < k:
            return False
        s = list(self.delta_raw)[-k:]
        inc = all(s[i] < s[i+1] for i in range(len(s)-1))
        dec = all(s[i] > s[i+1] for i in range(len(s)-1))
        return (self.position == "LONG" and dec) or (self.position == "SHORT" and inc)

    # ------------------- Signals -------------------
    def _signals(self, lp, bid_qty, ask_qty):
        reasons = []
        hh_hl = self._is_hh_hl(self.prices) if self.cfg["require_hh_hl_for_long"] else True
        ll_lh = self._is_ll_lh(self.prices) if self.cfg["require_ll_lh_for_short"] else True
        delta_inc = self._is_strictly_increasing(self.delta_raw, self.cfg["delta_trend_ticks"])
        delta_dec = self._is_strictly_decreasing(self.delta_raw, self.cfg["delta_trend_ticks"])
        imb_avg = sum(self.imbalance_raw) / len(self.imbalance_raw)
        depth_ratio = (bid_qty / max(1.0, ask_qty)) if (bid_qty and ask_qty) else 1.0

        absorp_ok_long, absorp_ok_short = self._absorption_ok()
        vwap = self.vwap_history[-1] if self.vwap_history else lp
        vwap_up = self.vwap_history[-1] > self.vwap_history[0] if len(self.vwap_history) >= 2 else True
        vwap_dn = self.vwap_history[-1] < self.vwap_history[0] if len(self.vwap_history) >= 2 else True

        long_ok = (
            hh_hl and delta_inc and
            (imb_avg >= self.cfg["imbalance_threshold_long"]) and
            (depth_ratio >= self.cfg["depth_ratio_min_long"]) and
            (not self.cfg["use_vwap_filter"] or (lp > vwap and vwap_up)) and
            (absorp_ok_long or True)
        )
        short_ok = (
            ll_lh and delta_dec and
            (imb_avg <= self.cfg["imbalance_threshold_short"]) and
            (depth_ratio <= self.cfg["depth_ratio_max_short"]) and
            (not self.cfg["use_vwap_filter"] or (lp < vwap and vwap_dn)) and
            (absorp_ok_short or True)
        )

        reasons.append(f"imb_avg={imb_avg:.2f} depth_ratio={depth_ratio:.2f} "
                       f"delta_inc={delta_inc} delta_dec={delta_dec} "
                       f"hh_hl={hh_hl} ll_lh={ll_lh} vwap={vwap:.2f}")
        return long_ok, short_ok, reasons

    # ------------------- Filters -------------------
    def _session_filters_pass(self) -> bool:
        now = dt.datetime.now(self.ctx.tz)
        mkt_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if (now - mkt_open).total_seconds() < self.cfg["skip_first_minutes"] * 60:
            return False

        def _in_range(t, s):
            h, m = map(int, s.split(":"))
            return t.hour > h or (t.hour == h and t.minute >= m)

        def _before(t, s):
            h, m = map(int, s.split(":"))
            return t.hour < h or (t.hour == h and t.minute < m)

        if _in_range(now, self.cfg["lunch_skip_start"]) and _before(now, self.cfg["lunch_skip_end"]):
            return False

        if self.cfg["use_best_windows"]:
            ok = False
            for a, b in self.cfg["best_windows"]:
                if _in_range(now, a) and _before(now, b):
                    ok = True
                    break
            if not ok:
                return False
        return True

    def _vwap_filter_pass(self, last_price: float) -> bool:
        if not self.vwap_history:
            return False
        vwap = self.vwap_history[-1]
        slope_up = vwap > self.vwap_history[0]
        slope_dn = vwap < self.vwap_history[0]
        above = last_price > vwap
        below = last_price < vwap
        return (above and slope_up) or (below and slope_dn)

    # ------------------- Diagnostics helper -------------------
    def _emit_diag(self, when_ts, lp, vwap, spread_ok, jump_ok, session_ok, vwap_ok,
                   imb_avg, depth_ratio, delta_slope, momentum_code, absorption_ok,
                   long_ok, short_ok, decision_action, position_state):
        try:
            diag = {
                "ts": when_ts.isoformat(),
                "symbol": self.symbol,
                "price": round(lp, 2) if lp is not None else None,
                "vwap": round(vwap, 2) if vwap is not None else None,
                "metrics": {
                    "imb_avg": round(imb_avg, 2) if imb_avg is not None else None,
                    "depth_ratio": round(depth_ratio, 2) if depth_ratio is not None else None,
                    "delta_slope": delta_slope,         # "up"|"down"|"flat"
                    "momentum": momentum_code,          # "HHHL"|"LLLH"|"-"
                    "absorption": bool(absorption_ok),
                },
                "filters": {
                    "session_ok": (None if session_ok is None else bool(session_ok)),
                    "vwap_ok": (None if vwap_ok is None else bool(vwap_ok)),
                    "spread_ok": bool(spread_ok),
                    "jump_ok": bool(jump_ok),
                },
                "signals": {
                    "long_ok": bool(long_ok),
                    "short_ok": bool(short_ok),
                },
                "decision": {
                    "action": decision_action  # "ENTER_LONG"|"ENTER_SHORT"|"EXIT"|"HOLD"
                },
                "position": {
                    "state": position_state,   # "FLAT"|"LONG"|"SHORT"
                    "entry_price": self.entry_price,
                }
            }
            self.ctx.report(self.symbol, diag)
        except Exception as e:
            self.ctx.log(f"[{self.symbol}] diag emit failed: {e}")

    # ------------------- Small utilities -------------------
    @staticmethod
    def _best_depth(depth: Dict[str, Any]):
        buy = (depth or {}).get("buy") or []
        sell = (depth or {}).get("sell") or []
        best_bid = buy[0]["price"] if buy else None
        best_ask = sell[0]["price"] if sell else None
        return best_bid, best_ask, buy, sell

    @staticmethod
    def _sum_qty(levels: List[Dict[str, Any]], n: int) -> float:
        return float(sum((levels[i]["quantity"] for i in range(min(n, len(levels)))), ))

    @staticmethod
    def _safe_float(x, default=None):
        try:
            return float(x)
        except Exception:
            return default

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
        if len(self.abs_range_prices) < max(3, int(self.cfg["absorption_window"])):
            return (False, False)
        rng = max(self.abs_range_prices) - min(self.abs_range_prices)
        mid = 0.5 * (max(self.abs_range_prices) + min(self.abs_range_prices))
        rng_bps = (rng / mid * 10000) if mid else 0.0
        traded = sum(self.abs_qty)
        ok = (traded >= self.cfg["absorption_min_traded_qty"]) and \
             (rng_bps <= self.cfg["absorption_max_price_range_bps"])
        return (ok, ok)

    # derived tags for telemetry
    def _delta_slope_tag(self, k: int) -> str:
        inc = self._is_strictly_increasing(self.delta_raw, k)
        dec = self._is_strictly_decreasing(self.delta_raw, k)
        if inc:
            return "up"
        if dec:
            return "down"
        return "flat"

    def _momentum_code(self) -> str:
        if self._is_hh_hl(self.prices):
            return "HHHL"
        if self._is_ll_lh(self.prices):
            return "LLLH"
        return "-"
