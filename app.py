from settings import load_config, TOKENS_PATH
from core.broker import Broker
import pprint

def main():
    b = Broker(...)
    token = b.load_access_token()
    if token:
        try:
            b.kite.set_access_token(token)
            # optional, but ignore failures:
            _ = b.profile()
        except Exception:
            pass
    # start whatever CLI you want, or just return

if __name__ == "__main__":   # ✅ only runs when invoked directly
    main()

