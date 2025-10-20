# strategies/oflt_config.py
"""
Centralized parameters for the OrderFlow + Momentum + Liquidity Trap strategy.

Tune these to your taste and risk. All values are read by the strategy at init time.
"""

CONFIG = {
    # ---------- CORE WINDOWS ----------
    # How many recent ticks we use to compute "micro momentum" (higher highs/lows)
    "momentum_window": 20,             # e.g., last 20 ticks

    # Over how many ticks to aggregate trade-direction delta (buy-initiated - sell-initiated)
    "delta_window": 12,                 # 5–20 typical

    # Over how many ticks to average the bid/ask imbalance metric
    "imbalance_window": 8,

    # How many levels from market depth to consider (1–5); 3 is a good balance
    "depth_levels": 3,

    # ---------- THRESHOLDS (ENTRY) ----------
    # A. Bid–Ask Imbalance threshold (avg over imbalance_window)
    #   +0.6 strong buyers, -0.6 strong sellers
    "imbalance_threshold_long": 0.60,
    "imbalance_threshold_short": -0.60,

    # B. Depth pressure ratio (sum of top-N bid qty / sum of top-N ask qty)
    #   > 1.5 favors long, < 1/1.5 favors short
    "depth_ratio_min_long": 1.50,
    "depth_ratio_max_short": 1 / 1.50,  # ~0.67

    # C. Delta trend: require delta to be strictly increasing/decreasing over last K ticks
    "delta_trend_ticks": 6,             # 5–10 good

    # D. Micro momentum requirement
    #   Require higher-highs & higher-lows for long, reverse for short
    "require_hh_hl_for_long": True,
    "require_ll_lh_for_short": True,

    # E. VWAP filter (trend alignment)
    #   Only LONG if price > VWAP and VWAP slope up; only SHORT if price < VWAP and VWAP slope down
    "use_vwap_filter": True,
    "vwap_slope_window": 30,            # ticks to check VWAP slope

    # ---------- LIQUIDITY TRAP / ABSORPTION ----------
    # We detect absorption: high traded size but small price progress over a short window
    "absorption_window": 15,            # ticks
    "absorption_min_traded_qty": 2000,  # sum of last-traded-qty over window
    "absorption_max_price_range_bps": 4, # max price move in basis points (0.04%)

    # ---------- PRE-TRADE FILTERS ----------
    # Trading session time filters (India time). Strings "HH:MM" 24h.
    "skip_first_minutes": 5,            # ignore first X minutes after 09:15
    "lunch_skip_start": "13:15",
    "lunch_skip_end":   "13:30",

    # Best trading windows (if you want to restrict)
    "use_best_windows": True,
    "best_windows": [("09:20", "13:15"), ("13:30", "14:45")],

    # Spread filter: skip if best ask - best bid > max_spread_pct * mid
    "max_spread_pct": 0.0005,           # 0.05%

    # News spikes / huge ticks: if absolute tick change > this (in bps), skip entry on that tick
    "max_tick_jump_bps": 10,            # 0.10%

    # ---------- RISK / EXECUTION ----------
    "dry_run": True,                    # keep True until you are confident
    "qty": 1,                           # per order
    "exchange": "NSE",                  # CO is supported for NSE EQ
    "stoploss_pct": 0.001,              # 0.10% SL trigger for the CO leg
    "cooldown_seconds": 60,             # min time between entries per symbol
    "max_trades_per_session": 3,        # cap number of entries per symbol per day

    # Optional time-based exit (requires a dedicated exit flow for COs)
    "time_stop_seconds": 180,           # if no follow-through for 3 minutes, consider exit (TODO)

    # ---------- LOGGING / DEBUG ----------
    "log_signals": True,                # log evaluated signals
    "log_rejections": True,             # log why a setup was rejected

        # ---------- EXITS ----------
    # Profit target in % (e.g., 0.001 = 0.10%)
    "target_pct": 0.001,
    # time_stop_seconds already present above; used as time-based exit

}
