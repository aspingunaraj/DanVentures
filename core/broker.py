import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException


class Broker:
    """
    A unified Zerodha broker wrapper that supports:
    ✅ Automatic token loading
    ✅ CLI-based interactive login (b.login())
    ✅ UI-based login (via /connect + /redirect and exchange_request_token)
    """

    def __init__(self, api_key: str, api_secret: str, redirect_uri: str, tokens_path: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri
        self.tokens_path = tokens_path
        self.kite = KiteConnect(api_key=api_key)

        # If a token already exists, load it so self.kite is authenticated
        token = self.load_access_token()
        if token:
            self.kite.set_access_token(token)

    # ----------------------------------------------------------------------
    # Token load/save
    # ----------------------------------------------------------------------
    def load_access_token(self) -> str | None:
        """Load access_token from tokens.json, if available."""
        if os.path.exists(self.tokens_path):
            try:
                data = json.load(open(self.tokens_path))
                return data.get("access_token")
            except Exception:
                return None
        return None

    def save_access_token(self, token: str) -> None:
        """Persist access_token to tokens.json."""
        json.dump({"access_token": token}, open(self.tokens_path, "w"))

    # ----------------------------------------------------------------------
    # UI-driven login flow helpers
    # ----------------------------------------------------------------------
    def login_url(self) -> str:
        """Generate the Zerodha login URL to redirect the user."""
        return self.kite.login_url()

    def exchange_request_token(self, request_token: str) -> str:
        """
        Exchange the request_token (obtained from redirect) for an
        access_token, set it on the kite client, and save it.
        Used in web-based flow (Flask).
        """
        try:
            session = self.kite.generate_session(request_token, api_secret=self.api_secret)
        except KiteException as e:
            raise RuntimeError(f"generate_session failed: {e}") from e

        access_token = session["access_token"]
        self.kite.set_access_token(access_token)
        self.save_access_token(access_token)
        return access_token

    # ----------------------------------------------------------------------
    # CLI-based (interactive) login flow (legacy / fallback)
    # ----------------------------------------------------------------------
    def _interactive_login(self) -> str:
        """
        Open the Zerodha login in a browser, capture the request_token via
        a local HTTP server, then exchange for access_token.
        """
        # 1) Redirect user to Zerodha login page
        login_url = self.kite.login_url()
        webbrowser.open(login_url)

        # 2) Local server captures ?request_token=
        holder = {"request_token": None}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != urlparse(self.server.redirect_uri).path:
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                holder["request_token"] = qs.get("request_token", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Login success. You can close this tab.")

            @property
            def server(self):
                return super().server

            def log_message(self, *args):
                """Silence HTTP server logs."""
                pass

        srv = HTTPServer(("127.0.0.1", 8000), Handler)
        srv.redirect_uri = self.redirect_uri

        srv.handle_request()

        request_token = holder["request_token"]
        if not request_token:
            raise RuntimeError("No request_token received. Check redirect_uri setting.")

        # 3) Exchange for access_token
        return self.exchange_request_token(request_token)

    def login(self) -> str:
        """
        Public method for CLI login.

        ✅ If token already saved, return it.
        ✅ Else do interactive browser login and save the new token.

        This preserves backward compatibility with the old flow.
        """
        tok = self.load_access_token()
        if tok:
            return tok
        return self._interactive_login()

    # ----------------------------------------------------------------------
    # Optional sanity check
    # ----------------------------------------------------------------------
    def profile(self):
        """Return the user profile to verify successful login."""
        return self.kite.profile()

    # ----------------------------------------------------------------------
    # Market data helpers
    # ----------------------------------------------------------------------
    def ltp(self, exchange: str, symbol: str):
        """Get LTP (Last Traded Price) of a symbol."""
        return self.kite.ltp(f"{exchange}:{symbol}")

    def instruments(self):
        """Fetch full instrument dump."""
        return self.kite.instruments()

    def historical(self, instrument_token: int, from_dt, to_dt, interval: str):
        """Fetch historical candles."""
        return self.kite.historical_data(instrument_token, from_dt, to_dt, interval)

    # ----------------------------------------------------------------------
    # Order helpers
    # ----------------------------------------------------------------------
    def place_market_order(self, exchange: str, symbol: str, side: str, qty: int, product: str = "MIS"):
        """Place a basic market order (regular variety)."""
        txn_type = self.kite.TRANSACTION_TYPE_BUY if side.upper() == "BUY" else self.kite.TRANSACTION_TYPE_SELL
        return self.kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn_type,
            quantity=int(qty),
            product=product,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

    def place_cover_order(
        self,
        exchange: str,
        symbol: str,
        side: str,                 # "BUY" or "SELL"
        qty: int,
        trigger_price: float,      # mandatory SL trigger for CO child leg
        order_type: str = "MARKET",# "MARKET" or "LIMIT" (entry order)
        price: float | None = None
    ):
        """
        Place a Cover Order (CO) on Zerodha.

        Notes:
        - CO must use variety='co' and product='MIS'.
        - 'trigger_price' is required and represents the SL-M trigger for the child order.
        - For LIMIT entry, set order_type='LIMIT' and pass 'price'.
        - Typically supported on NSE equities; make sure symbol/exchange is valid for CO.
        """
        txn_type = self.kite.TRANSACTION_TYPE_BUY if side.upper() == "BUY" else self.kite.TRANSACTION_TYPE_SELL

        params = dict(
            variety="co",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn_type,
            quantity=int(qty),
            product="MIS",
            order_type=self.kite.ORDER_TYPE_MARKET if order_type.upper() == "MARKET"
                       else self.kite.ORDER_TYPE_LIMIT,
            trigger_price=float(trigger_price),
        )

        if order_type.upper() == "LIMIT":
            if price is None:
                raise ValueError("price is required when order_type='LIMIT' for cover orders")
            params["price"] = float(price)

        return self.kite.place_order(**params)
