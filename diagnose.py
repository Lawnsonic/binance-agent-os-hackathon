"""
Diagnostic companion to scanner.py.

Answers two questions the scanner cannot:
  1. Is the pipeline actually working, or silently dropping everything?
  2. What does the real funding distribution look like right now, so
     thresholds get set from data instead of from a guess?
"""

import time
import requests

# The cost model is imported, never redefined. This file used to carry its
# own copy of the taker fees and the friction term, and it drifted: after the
# fee and basis corrections landed in scanner.py it was still printing 35.0
# bps while the scanner printed 44.32. Two different cost-to-beat figures on
# screen in the same session is a contradiction a careful reader is entitled
# to catch, and it can only be prevented structurally. The derivations, with
# the calls and fills that produced them, live in the scanner comments.
from scanner import ROUND_TRIP, FRICTION

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
TIMEOUT = 10

TOTAL_COST = ROUND_TRIP + FRICTION


def get(url, **params):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def main():
    info = get(f"{SAPI}/api/v3/exchangeInfo")
    spot = {s["symbol"] for s in info["symbols"]
            if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}

    snap = get(f"{FAPI}/fapi/v1/premiumIndex")
    now_ms = int(time.time() * 1000)

    total = len(snap)
    with_spot = [r for r in snap if r["symbol"] in spot]
    positive = [r for r in with_spot if float(r.get("lastFundingRate") or 0) > 0]

    print(f"\nperps on futures:        {total}")
    print(f"  ...also on spot:       {len(with_spot)}")
    print(f"  ...funding positive:   {len(positive)}")
    print(f"\ntotal cost to beat:      {TOTAL_COST*10_000:.1f} bps\n")

    ranked = sorted(positive,
                    key=lambda r: float(r["lastFundingRate"]),
                    reverse=True)

    print(f"{'symbol':<14}{'per 8h':>10}{'periods to':>13}{'h->fund':>10}")
    print(f"{'':<14}{'(bps)':>10}{'break even':>13}{'':>10}\n")

    for r in ranked[:25]:
        rate = float(r["lastFundingRate"])
        bps = rate * 10_000
        periods = TOTAL_COST / rate if rate > 0 else 999
        hrs = (int(r["nextFundingTime"]) - now_ms) / 3_600_000
        print(f"{r['symbol']:<14}{bps:>10.3f}{periods:>13.1f}{hrs:>10.1f}")

    if ranked:
        best = float(ranked[0]["lastFundingRate"])
        days = (TOTAL_COST / best) / 3 if best > 0 else 0
        print(f"\nbest available: {best*10_000:.3f} bps per settlement")
        print(f"break-even hold on the best symbol: ~{days:.1f} days\n")


if __name__ == "__main__":
    main()
