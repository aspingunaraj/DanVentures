import pathlib
import tomllib  # Python 3.11+; if 3.10 use 'tomli'

ROOT = pathlib.Path(__file__).parent
CONFIG = ROOT / "config.toml"
TOKENS_PATH = ROOT / "tokens.json"

def load_config():
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)
