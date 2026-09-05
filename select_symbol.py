"""
Symbol selection for the mechanism test.

This does NOT pick a symbol by funding rate. scanner.py already established
that nothing on the board clears its cost-to-beat, so there is no funding
trade to take. What this picks is the symbol on which a two-legged hedge at
minimum size is least wrong. The threshold itself lives in scanner.py and is
deliberately not repeated here.

The problem it solves: spot and USD-M futures publish different lot filters
for the same asset. A hedge is opened on the futures leg, and the spot leg
is then truncated to the spot stepSize against the actual futures fill. The
difference is unhedged delta you are holding whether you meant to or not.

What the live board actually says: only 6 of 356 hedgeable pairs have a spot
step COARSER than their futures step, so for almost every symbol that
truncation is exactly zero. ACEUSDT is the real case, a 0.01 futures step
against a 0.1 spot step, so up to one full spot step, 0.1 ACE or roughly 36
bps of a $5 leg, can end up unhedged depending on where the futures quantity
lands. At this size truncation is not the dominant error.

What dominates is the cross-venue entry basis, typically 15-25 bps, paid the
instant both legs are on. That is the FRICTION constant in scanner.py.

The spot fee was the other suspect, on the theory that a BUY pays commission
in the base asset and so lands short. A live check on 2026-09-04 disproved
it for this account: commission settles in BNB at 7.5 bps, the base quantity
arrives intact, and the fee residual is zero. See venue.py for the calls.

So for every symbol listed on both venues this computes, from live filters
and live top-of-book:

  min_leg        smallest per-leg notional that satisfies BOTH venues
  fut_qty        what the futures leg would actually fill at
  spot_qty       fut_qty truncated to the spot step
  step_resid     fut_qty - spot_qty, the unhedged remainder, in base and USD
  worst_step     one spot lot step as bps of the leg, the bound on that
                 remainder at this size regardless of where price ticks
  fee_resid      zero on this account. Verified 2026-09-04: spot commission
                 settles in BNB, so a BUY delivers the full base quantity.
                 Reverts to the taker rate out of base if burn is off.
  spread         measured, both venues, both sides. Replaces the assumed
                 5 bps friction constant with a number from the book.

Ranked by the truncation bound (rounded up to whole bps, since a
sub-cent residual is noise), then by measured round-trip spread.

Public REST only, four calls, filters cached on disk. No auth, no MCP.

    python select_symbol.py                     # ranked shortlist
    python select_symbol.py --symbol DOGEUSDT   # full arithmetic for one
    python select_symbol.py --refresh           # bypass the filter cache
"""

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext

import venue

getcontext().prec = 28

BPS = Decimal("10000")
ZERO = Decimal("0")


def bps(part, whole):
    if whole <= 0:
        return Decimal("99999")
    return part / whole * BPS


def plan(fsym, ssym, fbook, sbook, target):
    """
    Size one hedge exactly the way the executor will size it.

    Futures leg first (it has the coarser minimum), then the spot leg
    truncated to the spot step against the futures quantity. Returns None
    if no size satisfies both venues.
    """
    # We SELL the perp, so we hit the futures bid. We BUY spot, so we lift
    # the spot ask. Sizing off the price we will actually pay, not the mid.
    p_fut = fbook.bid
    p_spot = sbook.ask
    if p_fut <= 0 or p_spot <= 0:
        return None

    # Smallest futures quantity that satisfies the futures filters.
    f_min = max(fsym.min_qty, venue.ceil_step(fsym.min_notional / p_fut, fsym.step))
    f_min = venue.ceil_step(f_min, fsym.step)

    # Smallest spot quantity that satisfies the spot filters. Because this is
    # already a multiple of the spot step, any futures quantity >= it will
    # still clear the spot floor after truncation.
    s_min = max(ssym.min_qty, ssym.min_notional / p_spot)
    s_min = venue.ceil_step(s_min, ssym.step)

    q_min = venue.ceil_step(max(f_min, s_min), fsym.step)
    min_leg = q_min * p_fut

    # Trade the requested target if it is feasible, otherwise the minimum.
    q = venue.floor_step(target / p_fut, fsym.step)
    scaled_up = False
    if q < q_min:
        q = q_min
        scaled_up = True

    spot_q = venue.floor_step(q, ssym.step)
    if spot_q < ssym.min_qty or spot_q * p_spot < ssym.min_notional:
        return None                      # unreachable given q_min, belt and braces

    notional = q * p_fut
    spot_notional = spot_q * p_spot
    # What it costs to put the hedge on: we lift the spot ask and hit the
    # futures bid at the same instant, so the position is underwater by the
    # cross-venue basis before any fee is charged.
    entry_basis_bps = bps(p_spot - p_fut, p_fut)

    step_resid = q - spot_q                          # net SHORT this much base
    step_resid_usd = step_resid * p_spot
    # Verified 2026-09-04, not assumed: this account pays spot commission in
    # BNB (see venue.py), so a BUY delivers the full base quantity and the
    # long leg does not land short. Zero here is a measurement, not a hope.
    # If burn were off, or the BNB reserve ran to dust, this reverts to the
    # full taker rate coming out of the base asset.
    if venue.FEE_PAID_IN_BASE_ASSET:
        fee_resid = spot_q * venue.SPOT_TAKER
    else:
        fee_resid = ZERO
    fee_resid_usd = fee_resid * p_spot

    return {
        "symbol": fsym.symbol,
        "base": fsym.base,
        "fut_price": p_fut,
        "spot_price": p_spot,
        "fut_step": fsym.step,
        "spot_step": ssym.step,
        "fut_min_notional": fsym.min_notional,
        "spot_min_notional": ssym.min_notional,
        "min_leg": min_leg,
        "scaled_up": scaled_up,
        "fut_qty": q,
        "spot_qty": spot_q,
        "notional": notional,
        "spot_notional": spot_notional,
        "capital": notional + spot_notional,         # 1x futures margin + spot cost
        "step_resid": step_resid,
        "step_resid_usd": step_resid_usd,
        "step_resid_bps": bps(step_resid_usd, notional),
        "worst_step_bps": bps(ssym.step * p_spot, notional),
        "fee_resid_usd": fee_resid_usd,
        "fee_resid_bps": bps(fee_resid_usd, notional),
        "net_resid_bps": bps(step_resid_usd + fee_resid_usd, notional),
        "entry_basis_bps": entry_basis_bps,
        "spot_spread_bps": sbook.spread_bps,
        "fut_spread_bps": fbook.spread_bps,
        "rt_spread_bps": sbook.spread_bps + fbook.spread_bps,
        # Open lifts the spot ask and hits the futures bid; close does the
        # reverse. All four sides have to hold our size at top of book.
        "cover": min(
            sbook.ask_qty / spot_q, sbook.bid_qty / spot_q,
            fbook.bid_qty / q, fbook.ask_qty / q,
        ),
    }


def build(target, budget, min_cover, max_basis, refresh):
    t0 = time.time()
    spot, spot_age = venue.spot_specs(refresh=refresh)
    fut, fut_age = venue.futures_specs(refresh=refresh)
    sbooks = venue.spot_books()
    fbooks = venue.futures_books()
    elapsed = time.time() - t0

    # A matching symbol string is not proof the two venues quote the same
    # unit. Futures 1000X contracts in particular are a different base asset
    # from their spot listing, and hedging one with the other is not a hedge.
    both = sorted(sym for sym in set(spot) & set(fut)
                  if spot[sym].base == fut[sym].base)
    mismatched = len(set(spot) & set(fut)) - len(both)
    rows, rejected = [], {"no_book": 0, "infeasible": 0, "over_budget": 0,
                          "thin": 0, "dislocated": 0}
    all_feasible = []

    for sym in both:
        sb, fb = sbooks.get(sym), fbooks.get(sym)
        if sb is None or fb is None:
            rejected["no_book"] += 1
            continue
        p = plan(fut[sym], spot[sym], fb, sb, target)
        if p is None:
            rejected["infeasible"] += 1
            continue
        all_feasible.append(p)
        if p["capital"] > budget:
            rejected["over_budget"] += 1
            continue
        if p["cover"] < min_cover:
            rejected["thin"] += 1
            continue
        # A large gap between the two venues is either a genuine dislocation
        # or a stale book. Either way we would eat it on entry, so it is not
        # a candidate for a test whose whole point is a clean hedge.
        if abs(p["entry_basis_bps"]) > max_basis:
            rejected["dislocated"] += 1
            continue
        rows.append(p)

    # A truncation bound under 1 bp of a $5 leg is under half a cent: those
    # symbols are equivalent on residual, so the tie is broken on the cost we
    # can actually measure, the spread we have to cross on all four fills.
    rows.sort(key=lambda r: (r["worst_step_bps"].to_integral_value(rounding="ROUND_CEILING"),
                             r["rt_spread_bps"],
                             r["net_resid_bps"]))
    # How often do the two venues actually disagree on lot size? This is the
    # number the whole utility exists to establish, so report it over every
    # feasible pair, not just the ones that survived the budget filters.
    lumpy = [p for p in all_feasible if p["step_resid"] > 0]
    worst = max((p["step_resid_bps"] for p in lumpy), default=ZERO)

    return rows, {
        "universe_spot": len(spot), "universe_fut": len(fut), "both": len(both),
        "base_mismatch": mismatched, "feasible": len(all_feasible),
        "with_truncation_residual": len(lumpy),
        "worst_truncation_bps": float(worst),
        "rejected": rejected, "elapsed_s": round(elapsed, 2),
        "spot_filters_age_s": int(spot_age), "fut_filters_age_s": int(fut_age),
    }


def print_table(rows, top):
    print(f"\n{'symbol':<13}{'price':>11}{'leg $':>8}{'worst':>8}{'step':>8}"
          f"{'fee':>7}{'net':>8}{'entry':>8}{'spread':>8}{'cover':>7}{'cap $':>8}")
    print(f"{'':<13}{'':>11}{'':>8}{'step b':>8}{'resid b':>8}"
          f"{'b':>7}{'resid b':>8}{'basis b':>8}{'rt b':>8}{'x':>7}{'':>8}")
    print("-" * 94)
    for r in rows[:top]:
        print(f"{r['symbol']:<13}"
              f"{float(r['fut_price']):>11.6g}"
              f"{float(r['notional']):>8.2f}"
              f"{float(r['worst_step_bps']):>8.1f}"
              f"{float(r['step_resid_bps']):>8.1f}"
              f"{float(r['fee_resid_bps']):>7.1f}"
              f"{float(r['net_resid_bps']):>8.1f}"
              f"{float(r['entry_basis_bps']):>8.1f}"
              f"{float(r['rt_spread_bps']):>8.1f}"
              f"{float(r['cover']):>7.1f}"
              f"{float(r['capital']):>8.2f}")


def print_detail(r):
    f_step = venue.fmt_qty(r["fut_qty"], r["fut_step"])
    s_step = venue.fmt_qty(r["spot_qty"], r["spot_step"])
    print(f"\n=== {r['symbol']} ===")
    if r["scaled_up"]:
        print("  target below the joint minimum; scaled up to the smallest "
              "size both venues accept")
    print(f"  futures  bid {r['fut_price']}   step {r['fut_step']}   "
          f"minNotional {r['fut_min_notional']}")
    print(f"  spot     ask {r['spot_price']}   step {r['spot_step']}   "
          f"minNotional {r['spot_min_notional']}")
    print(f"\n  leg 1  SELL {f_step} {r['base']} perp   "
          f"= {float(r['notional']):.4f} USDT")
    print(f"  leg 2  BUY  {s_step} {r['base']} spot   "
          f"= {float(r['spot_notional']):.4f} USDT")
    print(f"         (leg 2 is leg 1 truncated to the spot step of {r['spot_step']})")
    print(f"\n  step residual   {venue.fmt_qty(r['step_resid'], r['spot_step'])} "
          f"{r['base']}  = {float(r['step_resid_usd']):.4f} USDT  "
          f"= {float(r['step_resid_bps']):.1f} bps of the leg")
    # Label only. The zero comes from venue.FEE_PAID_IN_BASE_ASSET, which was
    # settled on 2026-09-04 by two fills whose commissionAsset was BNB. It
    # reverts to an assumption only under the condition named in the scanner
    # comment: BNB burn switched off, or the BNB reserve run down to dust.
    print(f"  fee residual    {float(r['fee_resid_usd']):.4f} USDT  "
          f"= {float(r['fee_resid_bps']):.1f} bps   "
          f"[VERIFIED 2026-09-04: commission paid in BNB, "
          f"{r['base']} untouched]")
    print(f"  net unhedged    {float(r['net_resid_bps']):.1f} bps, short {r['base']}")
    eb = float(r["entry_basis_bps"])
    side = "underwater" if eb > 0 else "ahead"
    print(f"\n  entry basis     {eb:+.1f} bps spot ask vs futures bid")
    print(f"                  the hedge starts {side}, before a single fee "
          f"is charged")
    print(f"\n  measured spread  spot {float(r['spot_spread_bps']):.2f} bps   "
          f"futures {float(r['fut_spread_bps']):.2f} bps   "
          f"round trip {float(r['rt_spread_bps']):.2f} bps")
    print(f"  top-of-book cover {float(r['cover']):.1f}x our size on all four sides")
    print(f"\n  capital required  {float(r['capital']):.2f} USDT "
          f"({float(r['notional']):.2f} futures margin at 1x "
          f"+ {float(r['spot_notional']):.2f} spot)")
    print(f"  smallest feasible leg on this symbol: {float(r['min_leg']):.2f} USDT\n")


def to_json(r):
    out = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
    # Exactly the strings the executor should send, already step-correct.
    out["order"] = {
        "leg1": {"venue": "futures_usds", "symbol": r["symbol"], "side": "SELL",
                 "type": "MARKET", "quantity": venue.fmt_qty(r["fut_qty"], r["fut_step"])},
        "leg2": {"venue": "spot", "symbol": r["symbol"], "side": "BUY",
                 "type": "MARKET", "quantity": venue.fmt_qty(r["spot_qty"], r["spot_step"]),
                 "note": "resize against the ACTUAL leg1 fill before sending"},
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--target", type=Decimal, default=Decimal("5"),
                    help="intended per-leg notional in USDT (default 5)")
    ap.add_argument("--budget", type=Decimal, default=Decimal("15"),
                    help="total USDT available across both wallets (default 15)")
    ap.add_argument("--min-cover", type=Decimal, default=Decimal("2"),
                    help="required top-of-book size as a multiple of our own (default 2)")
    ap.add_argument("--max-basis-bps", type=Decimal, default=Decimal("50"),
                    help="reject pairs whose venues disagree by more than this (default 50)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-notional-only", action="store_true",
                    help="size every candidate at its own joint minimum, not --target")
    ap.add_argument("--symbol", help="print full arithmetic for one symbol")
    ap.add_argument("--refresh", action="store_true", help="bypass the filter cache")
    ap.add_argument("--json", default="shortlist.json",
                    help="where to write the machine-readable shortlist")
    args = ap.parse_args()

    # --min-notional-only sizes every candidate at its own joint minimum:
    # a target of zero never clears q_min, so every row scales up to it.
    target = Decimal("0") if args.min_notional_only else args.target
    rows, meta = build(target, args.budget, args.min_cover,
                       args.max_basis_bps, args.refresh)

    print(f"\nspot USDT pairs {meta['universe_spot']} | "
          f"USD-M perps {meta['universe_fut']} | listed on both {meta['both']}")
    print(f"filters cached {meta['spot_filters_age_s']}s / {meta['fut_filters_age_s']}s old, "
          f"data fetched in {meta['elapsed_s']}s")
    r = meta["rejected"]
    print(f"dropped: {r['no_book']} no two-sided book, {r['infeasible']} no feasible size, "
          f"{r['over_budget']} over {args.budget} USDT budget, "
          f"{r['thin']} thinner than {args.min_cover}x at top of book, "
          f"{r['dislocated']} with venues more than {args.max_basis_bps} bps apart")
    print(f"dropped {meta['base_mismatch']} more where the two venues quote a "
          f"different base asset for the same symbol string")
    print(f"remaining candidates: {len(rows)}")
    n_resid = meta["with_truncation_residual"]
    print(f"\nof {meta['feasible']} pairs that can be hedged at all, "
          f"{n_resid} {'leaves' if n_resid == 1 else 'leave'} a lot-step residual; "
          f"worst is {meta['worst_truncation_bps']:.0f} bps of one leg")

    if args.symbol:
        hit = next((x for x in rows if x["symbol"] == args.symbol.upper()), None)
        if hit is None:
            print(f"\n{args.symbol.upper()} is not a viable candidate under these "
                  f"constraints (or is not listed on both venues).\n")
            return 1
        print_detail(hit)
        return 0

    if not rows:
        print("\nNo symbol supports a two-legged hedge within this budget.\n")
        return 1

    print_table(rows, args.top)
    print_detail(rows[0])

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"target_leg_usdt": str(args.target), "budget_usdt": str(args.budget),
                   "min_cover": str(args.min_cover)},
        "meta": meta,
        "ranked_by": "worst_step_bps rounded up to whole bps, then "
                     "rt_spread_bps, then net_resid_bps",
        "candidates": [to_json(x) for x in rows[:args.top]],
    }
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"shortlist written to {args.json} "
          f"({len(payload['candidates'])} candidates, prices are a snapshot)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
