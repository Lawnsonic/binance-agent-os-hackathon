"""
Funding-rate divergence scanner.

Finds USD-M perpetuals whose funding rate is extreme enough that a
delta-neutral position (long spot / short perp) earns more in funding
than the full round-trip cost of opening and closing both legs.

Public endpoints only. No auth, no API key, no MCP.
Execution is handled separately via the Binance MCP server.
"""

import time
import requests

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"

# --- Cost model -------------------------------------------------------
# Taker fees, measured on this account rather than taken from the schedule.
#
# This was 0.0010, the standard spot rate. Wrong for this account. Verified
# 2026-09-04 ~17:45 UTC by live MCP call, in this order:
#
#   margin.getBnbBurnStatus          -> {"spotBNBBurn": true}
#   spot.getAccount                  -> BNB free 0.01399550, not dust
#   spot.accountCommission STRKUSDT  -> standardCommission.taker 0.00100000,
#        discount {enabledForAccount: true, discountAsset: "BNB",
#                  discount: "0.75000000"}
#
# That discount field is ambiguous: the docs gloss it as "reduced by this
# rate", which would make 0.75 a 75% cut and the rate 2.5 bps. Two settled
# fills resolve it arithmetically, and both agree:
#
#   spot.myTrades BNBUSDT   0.00000450 / 0.006 = 0.00075
#   the funding sell        0.00000675 / 0.009 = 0.00075
#
# So 0.75 is the MULTIPLIER on the standard rate, not the reduction, and the
# effective spot taker is 7.5 bps. commissionAsset on both fills is BNB, so
# the fee is not deducted from the base asset and the spot leg arrives at
# full quantity. Re-verify if BNB burn is switched off or the BNB reserve
# runs to dust, because either reverts this to 10 bps out of the base asset.
SPOT_TAKER = 0.00075     # 7.5 bps = 10 bps standard x 0.75 BNB discount
FUT_TAKER = 0.0005       # 0.05% per side

# Enter both legs, exit both legs = four taker fills.
ROUND_TRIP = (SPOT_TAKER * 2) + (FUT_TAKER * 2)   # 0.0025 = 25 bps

# Execution friction, taken off the live board instead of guessed.
#
# This was 0.0005, a guess covering "slippage + lot-step residual". That
# premise was wrong: select_symbol.py measured the live board and found only
# 6 of 356 hedgeable pairs have a spot step coarser than their futures step,
# so cross-venue truncation is exactly zero on almost every symbol and is
# nowhere near the dominant error term.
#
# What actually costs you is the cross-venue entry basis: the spot ask sits
# above the futures bid, so a hedge is underwater the instant both legs are
# on, before a single fee is charged. Measured 2026-09-04 17:40 UTC on
# STRKUSDT, the cheapest candidate on the board at that moment:
#
#     entry basis        19.32 bps   (spot ask vs futures bid, one way)
#     round-trip spread   7.72 bps   (spot 3.86 + futures 3.86, both venues)
#
# The entry basis is the number used. It is the larger of the two and it is
# what you pay on the way in; erring high can only make the scanner refuse
# more, never manufacture a trade.
#
# Re-derive with:  python select_symbol.py --refresh
# It is a snapshot of one moment. Rerun it before quoting this figure.
FRICTION = 0.001932

# How many 8h funding settlements we intend to hold through.
HOLD_PERIODS = 3

# Required profit above cost before we call it a trade.
MIN_EDGE = 0.0010        # 10 bps

# Funding must have held the same sign this many prints running.
PERSISTENCE = 3

TIMEOUT = 10


def get(url, **params):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def spot_universe():
    """Symbols tradable on spot, so we can actually build the long leg."""
    info = get(f"{SAPI}/api/v3/exchangeInfo")
    return {
        s["symbol"]
        for s in info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    }


def funding_snapshot():
    """Current funding rate and mark price for every perp."""
    return get(f"{FAPI}/fapi/v1/premiumIndex")


def funding_history(symbol, limit=PERSISTENCE):
    return get(f"{FAPI}/fapi/v1/fundingRate", symbol=symbol, limit=limit)


def persistent(symbol, current_rate):
    """
    One extreme print is noise. We want the same sign holding across
    the last few settlements before we commit capital to it.
    """
    try:
        hist = funding_history(symbol)
    except Exception:
        return False
    if len(hist) < PERSISTENCE:
        return False
    sign = 1 if current_rate > 0 else -1
    return all((1 if float(h["fundingRate"]) > 0 else -1) == sign for h in hist)


def scan():
    spot = spot_universe()
    snap = funding_snapshot()
    now_ms = int(time.time() * 1000)

    candidates = []
    for row in snap:
        sym = row["symbol"]

        # Need a spot leg to hedge against.
        if sym not in spot:
            continue

        rate = float(row.get("lastFundingRate") or 0)

        # Long spot / short perp collects funding only when it is positive.
        if rate <= 0:
            continue

        gross = rate * HOLD_PERIODS
        net = gross - ROUND_TRIP - FRICTION
        if net < MIN_EDGE:
            continue

        hours_to_funding = (int(row["nextFundingTime"]) - now_ms) / 3_600_000
        # Entering right before settlement means paying full cost for a
        # sliver of funding. Give ourselves room.
        if hours_to_funding < 1.0:
            continue

        if not persistent(sym, rate):
            continue

        candidates.append({
            "symbol": sym,
            "funding_bps": rate * 10_000,
            "gross_bps": gross * 10_000,
            "net_bps": net * 10_000,
            "hours_to_funding": hours_to_funding,
            "mark": float(row["markPrice"]),
        })
        time.sleep(0.05)   # be polite to the weight limit

    return sorted(candidates, key=lambda c: c["net_bps"], reverse=True)


def report():
    print(f"\ncost model: round-trip {ROUND_TRIP*10_000:.0f}bps "
          f"+ friction {FRICTION*10_000:.0f}bps, "
          f"hold {HOLD_PERIODS} periods, min edge {MIN_EDGE*10_000:.0f}bps")

    rows = scan()
    if not rows:
        print("\nNO QUALIFYING SIGNAL. Funding does not clear cost. Not trading.\n")
        return

    print(f"\n{'symbol':<14}{'fund':>9}{'gross':>9}{'net':>9}{'h->fund':>9}")
    for c in rows[:15]:
        print(f"{c['symbol']:<14}"
              f"{c['funding_bps']:>8.2f}b"
              f"{c['gross_bps']:>8.2f}b"
              f"{c['net_bps']:>8.2f}b"
              f"{c['hours_to_funding']:>8.1f}h")
    print()


if __name__ == "__main__":
    report()
