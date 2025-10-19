# core/stream.py
import logging
import threading
import time
from typing import Callable, Iterable, Dict, List
from kiteconnect import KiteTicker

log = logging.getLogger(__name__)


class TickBus:
    """Minimal pub/sub bus for broadcasting tick lists to subscribers."""
    def __init__(self):
        self._subs: List[Callable[[List[dict]], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[List[dict]], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def publish(self, ticks: List[dict]) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(ticks)
            except Exception:
                log.exception("Tick subscriber raised")


class Stream:
    """
    One WebSocket connection that:
      • subscribes to instrument tokens
      • publishes ticks via TickBus
      • handles reconnect across KiteTicker versions
    """
    def __init__(self, api_key: str, access_token: str, tokens: Iterable[int], bus: TickBus, mode: str = "FULL"):
        self.api_key = api_key
        self.access_token = access_token
        self.tokens = sorted(set(int(t) for t in tokens))
        self.bus = bus
        self.mode = (mode or "FULL").upper()

        self.kws = KiteTicker(self.api_key, self.access_token)
        self.kws.on_connect = self._on_connect
        self.kws.on_ticks = self._on_ticks
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect
        self.kws.on_noreconnect = self._on_noreconnect

        # --- Conditional reconnect setup (works across versions) ---
        if hasattr(self.kws, "enable_reconnect"):
            try:
                self.kws.enable_reconnect(reconnect=True, interval=5, retries=50)
            except Exception:
                log.warning("enable_reconnect() failed; falling back to attribute-based setup")
                self._fallback_reconnect_setup()
        else:
            self._fallback_reconnect_setup()

        self._connected = threading.Event()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def start(self, threaded: bool = True):
        """Start the WebSocket; returns immediately if threaded=True."""
        log.info("Starting KiteTicker…")
        self.kws.connect(threaded=threaded)
        if threaded:
            self._connected.wait(timeout=10)

    def stop(self):
        try:
            self.kws.close()
        except Exception:
            log.exception("Error closing websocket")

    def update_tokens(self, new_tokens: Iterable[int]):
        """Dynamically update the subscribed instruments set."""
        new = sorted(set(int(t) for t in new_tokens))
        add = [t for t in new if t not in self.tokens]
        rem = [t for t in self.tokens if t not in new]
        if rem:
            try:
                self.kws.unsubscribe(rem)
            except Exception:
                log.exception("Unsubscribe failed")
        if add:
            self._subscribe_in_chunks(add)
        self.tokens = new

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _fallback_reconnect_setup(self):
        """Attempt attribute-based reconnect configuration; ignore if absent."""
        try:
            if hasattr(self.kws, "reconnect"):
                self.kws.reconnect = True
            if hasattr(self.kws, "reconnect_max_tries"):
                self.kws.reconnect_max_tries = 50
            if hasattr(self.kws, "reconnect_min_delay"):
                self.kws.reconnect_min_delay = 1
            if hasattr(self.kws, "reconnect_max_delay"):
                self.kws.reconnect_max_delay = 5
        except Exception:
            log.debug("Reconnect attribute setup not supported by this KiteTicker version")

    @staticmethod
    def _chunk(seq: List[int], n: int):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    def _subscribe_in_chunks(self, tokens: List[int], chunk_size: int = 400):
        """Subscribe in chunks to avoid frame size/limit issues."""
        for chunk in self._chunk(tokens, chunk_size):
            try:
                self.kws.subscribe(chunk)
            except Exception:
                log.exception("Subscribe failed for chunk")
            time.sleep(0.05)

    # ------------------------------------------------------------------ #
    # KiteTicker callbacks
    # ------------------------------------------------------------------ #
    def _on_connect(self, ws, resp):
        log.info("WS connected. Subscribing to %d tokens…", len(self.tokens))
        self._connected.set()
        self._subscribe_in_chunks(self.tokens)
        try:
            if self.mode == "FULL":
                ws.set_mode(ws.MODE_FULL, self.tokens)
            else:
                ws.set_mode(ws.MODE_LTP, self.tokens)
        except Exception:
            log.exception("set_mode failed")

    def _on_ticks(self, ws, ticks: List[Dict]):
        if ticks:
            self.bus.publish(ticks)

    def _on_close(self, ws, code, reason):
        log.warning("WS closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws, code, reason):
        log.error("WS error: code=%s reason=%s", code, reason)

    def _on_reconnect(self, ws, attempt_count):
        log.warning("WS reconnecting… attempt=%s", attempt_count)

    def _on_noreconnect(self, ws):
        log.error("WS will NOT reconnect further (max retries hit)")
