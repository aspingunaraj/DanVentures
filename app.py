from settings import load_config, TOKENS_PATH
from core.broker import Broker
import pprint

def main():
    cfg = load_config()
    b = Broker(
        api_key=cfg["broker"]["api_key"],
        api_secret=cfg["broker"]["api_secret"],
        redirect_uri=cfg["broker"]["redirect_uri"],
        tokens_path=str(TOKENS_PATH),
    )
    access_token = b.login()
    print("✅ Logged in. access_token saved to tokens.json")

    # Sanity check: fetch profile
    prof = b.profile()
    print("Profile:")
    pprint.pprint(prof)

if __name__ == "__main__":
    main()
