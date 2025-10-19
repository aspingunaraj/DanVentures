from abc import ABC, abstractmethod

class StrategyBase(ABC):
    """
    Base class for all strategies.
    Each strategy instance handles ONE symbol.
    """
    def __init__(self, symbol, context=None):
        self.symbol = symbol
        self.ctx = context  # context will hold broker, config, state, etc.

    @abstractmethod
    def on_tick(self, tick: dict):
        pass

    def on_bar(self, bar: dict):
        """Optional: override for bar-based strategies."""
        pass
