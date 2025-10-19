# core/instruments.py
import csv
import os
from typing import Dict, Tuple
from settings import ROOT

INSTR_CACHE = ROOT / "data" / "instruments.csv"
INSTR_CACHE.parent.mkdir(parents=True, exist_ok=True)

def ensure_cache(kite) -> None:
    """Download instruments dump once and cache to CSV."""
    if os.path.exists(INSTR_CACHE):
        return
    rows = kite.instruments()  # list[dict]
    with open(INSTR_CACHE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instrument_token", "exchange", "tradingsymbol", "name"])
        for r in rows:
            w.writerow([r["instrument_token"], r["exchange"], r["tradingsymbol"], r.get("name", "")])

def build_maps() -> Tuple[Dict[str, int], Dict[int, str]]:
    """Return (symbol→token, token→symbol) using the cached CSV."""
    sym2tok: Dict[str, int] = {}
    tok2sym: Dict[int, str] = {}
    with open(INSTR_CACHE) as f:
        rd = csv.DictReader(f)
        for r in rd:
            if r["exchange"] not in ("NSE", "BSE"):
                continue
            sym = r["tradingsymbol"]
            tok = int(r["instrument_token"])
            sym2tok[sym] = tok
            tok2sym[tok] = sym
    return sym2tok, tok2sym
