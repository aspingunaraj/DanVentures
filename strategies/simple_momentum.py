from collections import deque
from strategies.base import StrategyBase

class SimpleMomentumStrategy(StrategyBase):
    def __init__(self, symbol, context, window=5, sl_pct=0.003, qty=1):
        super().__init__(symbol, context)
        self.window = window
        self.sl_pct = sl_pct
        self.qty = qty

        self.prices = deque(maxlen=window)
        self.position = None  # "LONG", "SHORT", or None

    def on_tick(self, tick: dict):
        if tick.get("instrument_token") != self.ctx.tokens[self.symbol]:
            return

        lp = tick.get("last_price")
        if lp is None:
            return

        self.prices.append(lp)
        if len(self.prices) < self.window:
            return

        increasing = all(self.prices[i] < self.prices[i+1] for i in range(self.window - 1))
        decreasing = all(self.prices[i] > self.prices[i+1] for i in range(self.window - 1))

        if increasing and self.position != "LONG":
            self.enter_long(lp)

        elif decreasing and self.position != "SHORT":
            self.enter_short(lp)

    def enter_long(self, entry_price):
        sl_trigger = round(entry_price * (1 - self.sl_pct), 2)
        self.ctx.log(f"[Strategy] BUY {self.symbol} @~{entry_price} CO SL={sl_trigger}")

        if self.ctx.dry:
            self.position = "LONG"
            return

        try:
            self.ctx.broker.place_cover_order(
                exchange=self.ctx.exchange,
                symbol=self.symbol,
                side="BUY",
                qty=self.qty,
                trigger_price=sl_trigger,
                order_type="MARKET",
            )
            self.position = "LONG"
        except Exception as e:
            self.ctx.log(f"[Strategy] BUY-CO failed for {self.symbol}: {e}")

    def enter_short(self, entry_price):
        sl_trigger = round(entry_price * (1 + self.sl_pct), 2)
        self.ctx.log(f"[Strategy] SELL {self.symbol} @~{entry_price} CO SL={sl_trigger}")

        if self.ctx.dry:
            self.position = "SHORT"
            return

        try:
            self.ctx.broker.place_cover_order(
                exchange=self.ctx.exchange,
                symbol=self.symbol,
                side="SELL",
                qty=self.qty,
                trigger_price=sl_trigger,
                order_type="MARKET",
            )
            self.position = "SHORT"
        except Exception as e:
            self.ctx.log(f"[Strategy] SELL-CO failed for {self.symbol}: {e}")
