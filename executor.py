"""
Two-legged hedge executor: fill-driven sizing and reconciliation.

Short the USD-M perp, buy the spot, at minimum size. This is a MECHANISM
TEST, not a trade. scanner.py refused the entire board; nothing here clears
cost. The point is to prove the execution path is real and to measure what
it actually costs, with every number coming off a live fill.

ARCHITECTURE, because it constrains everything below.

Orders go through the Binance MCP server, which is authenticated by OAuth in
the agent session. Python cannot call it: venue.py is unauthenticated public
REST and there are no API keys on disk. So this module does not place orders.
It does the arithmetic and holds the state; the agent places each order and
feeds the raw JSON response back in.

That costs something real, and the log says so instead of glossing it:
between leg one filling and leg two filling there is an agent round trip, and
for the length of it the position is one-sided and directionally exposed.
exposure_seconds in the report is that window, timed off the two fills.

THE ORDERING RULE (CONTEXT section 7.1) is the reason this is a state machine
and not one function. Leg one goes on first. Leg two is sized from the
ACTUAL filled quantity in leg one's response, truncated to spot's own step.
Never from the intended figure both were planned against. A market order can
fill short, and sizing leg two off the intention rather than the fill is how
you end up with a hedge that is not a hedge.

    python executor.py plan                     pick symbol, size leg 1
    python executor.py record-leg1  --response  read the fill, size leg 2
    python executor.py record-leg2  --response  reconcile, log residual
    python executor.py unwind                   leg 2 failed: shrink leg 1
    python executor.py close                    plan both closing orders
    python executor.py record-close --leg fut|spot --response
    python executor.py report                   full reconciliation
    python executor.py preflight                one unpaired futures order

--response takes a file path, a raw JSON string, or - for stdin.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import venue
import select_symbol

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "trade_state.json")
LOG_PATH = os.path.join(HERE, "trades.jsonl")

# Mandated shortlist. select_symbol.py ranks the whole board; execution is
# restricted to this cluster, cheapest at run time.
CLUSTER = ["STRKUSDT", "FLOWUSDT", "ZILUSDT", "HMSTRUSDT"]

BPS = Decimal("10000")
ZERO = Decimal("0")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def D(x):
    return Decimal(str(x))


def px(x):
    """Price for display. Stored values keep full precision; screens do not."""
    d = D(x)
    return format(d.normalize(), "f") if d == d.to_integral_value() else f"{float(d):.8g}"


# --- state ------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        sys.exit("no trade in progress - run: python executor.py plan")
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def log(event, payload):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event": event, **payload}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_response(arg):
    """A file path, a raw JSON string, or - for stdin."""
    if arg == "-":
        raw = sys.stdin.read()
    elif os.path.exists(arg):
        with open(arg, encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = arg
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse the order response as JSON: {e}")


# --- fill parsing -----------------------------------------------------

def parse_futures_fill(resp, query=None):
    """
    Pull the ACTUAL fill out of a futures_usds_newOrder response.

    executedQty is the number that matters. status FILLED is not assumed:
    a MARKET order can come back PARTIALLY_FILLED or EXPIRED with a
    partial, and the quantity we sent is not evidence of anything.

    Found by the preflight order on 2026-09-04, which is why the preflight
    exists: this MCP server's futures newOrder response carries executedQty
    and cumQty but NO avgPrice and NO cumQuote, even with
    newOrderRespType=RESULT. Live example, order 6578993465:

        {"status":"FILLED","executedQty":"182.8","cumQty":"182.8", ...}

    futures_usds_queryOrder on the same id does return them:

        {"avgPrice":"0.02737000","cumQuote":"5.00323600", ...}

    So the price has to be fetched separately and merged in here. Deriving
    it from the plan instead would be inventing a fill price, which is
    exactly what CONTEXT rule 4 forbids. If it is missing we say so and
    stop rather than recording a zero.
    """
    src = dict(resp)
    if query:
        src.update({k: v for k, v in query.items() if v not in (None, "")})

    executed = D(src.get("executedQty", "0"))
    quote = D(src.get("cumQuote", "0") or "0")
    avg = D(src.get("avgPrice", "0") or "0")
    if avg <= 0 and executed > 0 and quote > 0:
        avg = quote / executed
    if quote <= 0 and executed > 0 and avg > 0:
        quote = executed * avg

    return {
        "order_id": src.get("orderId"),
        "symbol": src.get("symbol"),
        "side": src.get("side"),
        "status": src.get("status"),
        "executed_qty": str(executed),
        "avg_price": str(avg),
        "quote_qty": str(quote),
        "price_missing": bool(executed > 0 and avg <= 0),
    }


def require_price(fill, order_id, symbol):
    """A fill with no price is not a recorded fill. Stop and ask for it."""
    if not fill.get("price_missing"):
        return
    sys.exit(
        f"\n  order {order_id} filled {fill['executed_qty']} but the response\n"
        f"  carried no avgPrice/cumQuote (known gap in this server's futures\n"
        f"  newOrder response). Refusing to record a fill with no price.\n\n"
        f"  fetch it:  futures_usds_queryOrder symbol={symbol} "
        f"orderId={order_id}\n"
        f"  then rerun this command with --query '<that json>'\n")


def parse_spot_fill(resp):
    """
    Same for spot_newOrder. Spot returns a fills[] array carrying the real
    commission and the asset it was taken in, which is how we confirm at
    execution time that the fee did not come out of the base asset.
    """
    executed = D(resp.get("executedQty", "0"))
    quote = D(resp.get("cummulativeQuoteQty", "0") or "0")
    avg = quote / executed if executed > 0 else ZERO

    commissions = {}
    for fill in resp.get("fills") or []:
        asset = fill.get("commissionAsset")
        if asset is None:
            continue
        commissions[asset] = str(D(commissions.get(asset, "0"))
                                 + D(fill.get("commission", "0")))

    return {
        "order_id": resp.get("orderId"),
        "symbol": resp.get("symbol"),
        "side": resp.get("side"),
        "status": resp.get("status"),
        "executed_qty": str(executed),
        "avg_price": str(avg),
        "quote_qty": str(quote),
        "commissions": commissions,
    }


# --- planning ---------------------------------------------------------

def pick(target, symbol=None):
    """Cheapest member of the mandated cluster on live top-of-book."""
    rows, meta = select_symbol.build(target, Decimal("15"), Decimal("2"),
                                     Decimal("50"), refresh=False)
    pool = [r for r in rows if r["symbol"] in CLUSTER]
    if symbol:
        pool = [r for r in rows if r["symbol"] == symbol.upper()]
        if not pool:
            sys.exit(f"{symbol.upper()} is not viable right now "
                     f"(check select_symbol.py for why)")
    if not pool:
        sys.exit(f"none of {CLUSTER} is currently viable - "
                 f"run select_symbol.py to see why")
    # Cheapest to execute = least spread to cross on all four fills.
    return sorted(pool, key=lambda r: r["rt_spread_bps"])[0], meta


def predicted_cost(row):
    """
    What the round trip should cost, from verified rates and the live book.
    Fees are the measured 7.5/5 bps, not the standard rates.
    """
    fees_bps = (venue.SPOT_TAKER * 2 + venue.FUT_TAKER * 2) * BPS
    spread_bps = row["rt_spread_bps"]
    return {
        "fees_bps": str(fees_bps),
        "spread_bps": str(spread_bps),
        "total_bps": str(fees_bps + spread_bps),
        "entry_basis_bps": str(row["entry_basis_bps"]),
    }


def cmd_plan(args):
    if os.path.exists(STATE_PATH):
        st = load_state()
        if st.get("phase") not in ("closed", "aborted"):
            sys.exit(f"a trade is already in phase '{st.get('phase')}'. "
                     f"finish or delete {STATE_PATH} first.")

    row, meta = pick(args.target, args.symbol)
    fut_qty = D(row["fut_qty"])
    order = {
        "tool": "futures_usds_newOrder",
        "symbol": row["symbol"],
        "side": "SELL",
        "type": "MARKET",
        "quantity": venue.fmt_qty(fut_qty, D(row["fut_step"])),
    }

    st = {
        "phase": "planned",
        "planned_at": now(),
        "mode": "MECHANISM_TEST",
        "note": ("No signal cleared cost. This is a plumbing test at "
                 "minimum size, not a trade taken on its merits."),
        "symbol": row["symbol"],
        "base": row["base"],
        # Everything is Decimal in transit and str on disk: JSON has no
        # Decimal, and going through float would reintroduce exactly the
        # rounding error venue.py exists to keep out.
        "fut_step": str(row["fut_step"]),
        "spot_step": str(row["spot_step"]),
        "planned": {k: str(row[k]) for k in
                    ("fut_qty", "spot_qty", "notional", "spot_notional",
                     "fut_price", "spot_price", "min_leg", "cover")},
        "predicted": predicted_cost(row),
        "leg1_order": order,
    }
    save_state(st)
    log("plan", {"symbol": row["symbol"], "order": order,
                 "predicted": st["predicted"]})

    print(f"\n  MECHANISM TEST - {row['symbol']}")
    print(f"  {st['note']}")
    print(f"\n  chosen from {CLUSTER}")
    print(f"  by cheapest round-trip spread: "
          f"{float(row['rt_spread_bps']):.2f} bps")
    print(f"\n  leg 1 (open first, per CONTEXT 7.1):")
    print(f"    {json.dumps(order)}")
    print(f"\n  intended notional   {float(row['notional']):.4f} USDT at 1x")
    print(f"  spot leg will be sized from the ACTUAL fill, not from this plan")
    print(f"  predicted round trip {float(D(st['predicted']['total_bps'])):.2f} bps "
          f"({float(D(st['predicted']['fees_bps'])):.2f} fees + "
          f"{float(D(st['predicted']['spread_bps'])):.2f} spread)")
    print(f"\n  place it, then: python executor.py record-leg1 --response <json>\n")
    return 0


def cmd_preflight(args):
    """
    One unpaired minimum-size futures order, to confirm the order path and
    the confirmation behaviour BEFORE a second leg depends on it. Finding a
    permission edge case with one leg already open is the failure this
    exists to prevent.
    """
    row, _ = pick(args.target, args.symbol)
    fut_qty = D(row["fut_qty"])
    order = {
        "tool": "futures_usds_newOrder",
        "symbol": row["symbol"],
        "side": "SELL",
        "type": "MARKET",
        "quantity": venue.fmt_qty(fut_qty, D(row["fut_step"])),
    }
    close = dict(order, side="BUY")
    print(f"\n  PREFLIGHT - unpaired, {row['symbol']}, "
          f"{float(row['notional']):.2f} USDT at 1x")
    print(f"  Opens a naked short. Directional until closed. Close it "
          f"immediately.\n")
    print(f"  open:   {json.dumps(order)}")
    print(f"  close:  {json.dumps(close)}")
    print(f"\n  margin required ~{float(row['notional']):.2f} USDT in the "
          f"USD-M wallet\n")
    log("preflight_plan", {"symbol": row["symbol"], "order": order})
    return 0


# --- leg one ----------------------------------------------------------

def cmd_record_leg1(args):
    st = load_state()
    if st["phase"] != "planned":
        sys.exit(f"expected phase 'planned', found '{st['phase']}'")

    query = read_response(args.query) if args.query else None
    fill = parse_futures_fill(read_response(args.response), query)
    require_price(fill, fill["order_id"], st["symbol"])
    st["leg1_fill"] = fill
    st["leg1_at"] = now()
    st["leg1_epoch"] = time.time()

    filled = D(fill["executed_qty"])
    if filled <= 0:
        st["phase"] = "aborted"
        save_state(st)
        log("leg1_empty", {"fill": fill})
        print(f"\n  leg 1 filled nothing (status {fill['status']}). "
              f"Nothing is open. Aborted.\n")
        return 1

    if fill["symbol"] != st["symbol"]:
        sys.exit(f"response is for {fill['symbol']}, state is {st['symbol']}")

    # THE RULE: leg two is sized off this number, not off the plan.
    spot_step = D(st["spot_step"])
    spot_qty = venue.floor_step(filled, spot_step)

    planned_fut = D(st["planned"]["fut_qty"])
    short_by = planned_fut - filled

    st["phase"] = "leg1_open"
    st["leg2_order"] = {
        "tool": "spot_newOrder",
        "symbol": st["symbol"],
        "side": "BUY",
        "type": "MARKET",
        "quantity": venue.fmt_qty(spot_qty, spot_step),
    }
    save_state(st)
    log("leg1_fill", {"fill": fill, "sized_leg2_from_fill": str(spot_qty)})

    print(f"\n  leg 1 FILLED  order {fill['order_id']}  "
          f"{fill['executed_qty']} {st['base']} @ {px(fill['avg_price'])}")
    print(f"  status {fill['status']}, notional {fill['quote_qty']} USDT")
    if short_by != 0:
        print(f"  NOTE: filled {short_by} short of the planned "
              f"{planned_fut}. Leg 2 follows the fill, not the plan.")
    print(f"\n  leg 2 sized from the actual fill:")
    print(f"    {filled} truncated to spot step {spot_step} = {spot_qty}")
    if spot_qty < filled:
        resid = filled - spot_qty
        print(f"    leaves {resid} {st['base']} that spot cannot express")
    print(f"\n    {json.dumps(st['leg2_order'])}")
    print(f"\n  POSITION IS ONE-SIDED UNTIL LEG 2 FILLS. Place it now.")
    print(f"  then: python executor.py record-leg2 --response <json>\n")
    return 0


# --- leg two ----------------------------------------------------------

def cmd_record_leg2(args):
    st = load_state()
    if st["phase"] != "leg1_open":
        sys.exit(f"expected phase 'leg1_open', found '{st['phase']}'")

    fill = parse_spot_fill(read_response(args.response))
    st["leg2_fill"] = fill
    st["leg2_at"] = now()
    st["exposure_seconds"] = round(time.time() - st["leg1_epoch"], 1)

    fut_qty = D(st["leg1_fill"]["executed_qty"])
    spot_qty = D(fill["executed_qty"])
    wanted = D(st["leg2_order"]["quantity"])

    residual = fut_qty - spot_qty
    # Not named px: that is the display helper, and shadowing it here made
    # this function crash on the print after the state was already saved.
    mark_px = D(fill["avg_price"]) if spot_qty > 0 else D(st["planned"]["spot_price"])
    residual_usd = residual * mark_px
    notional = D(st["leg1_fill"]["quote_qty"])

    st["residual"] = {
        "base": str(residual),
        "usd": str(residual_usd),
        "bps_of_leg": str(residual_usd / notional * BPS) if notional > 0 else None,
        "direction": "short" if residual > 0 else ("long" if residual < 0 else "flat"),
    }

    # Rule 7.3: a short or failed leg two means leg one gets reduced now,
    # not after a conversation about it.
    short = spot_qty < wanted
    st["phase"] = "hedged" if not short else "leg2_short"
    save_state(st)
    log("leg2_fill", {"fill": fill, "residual": st["residual"],
                      "exposure_seconds": st["exposure_seconds"]})

    print(f"\n  leg 2 FILLED  order {fill['order_id']}  "
          f"{fill['executed_qty']} {st['base']} @ {px(fill['avg_price'])}")
    print(f"  status {fill['status']}, notional {fill['quote_qty']} USDT")
    if fill["commissions"]:
        print(f"  commission {fill['commissions']}")
        if "BNB" in fill["commissions"]:
            print(f"    paid in BNB, so the base quantity arrived intact "
                  f"(confirms the venue.py fee check at execution time)")
        elif st["base"] in fill["commissions"]:
            print(f"    WARNING: commission came out of {st['base']}. The BNB "
                  f"discount did not apply. The long leg is short by that much.")

    print(f"\n  exposure window between legs: {st['exposure_seconds']}s one-sided")
    print(f"\n  RESIDUAL  {venue.fmt_qty(residual, D(st['spot_step']))} "
          f"{st['base']}  = {float(residual_usd):.6f} USDT")
    if notional > 0:
        print(f"            {float(residual_usd / notional * BPS):.2f} bps of the leg, "
              f"net {st['residual']['direction']} {st['base']}")

    if short:
        print(f"\n  LEG 2 FILLED SHORT: wanted {wanted}, got {spot_qty}.")
        print(f"  Directional exposure is open. Reduce leg 1 now:")
        print(f"    python executor.py unwind\n")
        return 1

    print(f"\n  hedged. close with: python executor.py close\n")
    return 0


def cmd_unwind(args):
    """Shrink leg one to match whatever leg two actually achieved."""
    st = load_state()
    if st["phase"] not in ("leg2_short", "leg1_open"):
        sys.exit(f"nothing to unwind from phase '{st['phase']}'")

    fut_qty = D(st["leg1_fill"]["executed_qty"])
    spot_qty = D(st.get("leg2_fill", {}).get("executed_qty", "0"))
    fut_step = D(st["fut_step"])

    # Buy back the unmatched part of the short. Round UP so we never leave
    # a sliver of naked short behind; overshooting into a tiny long is the
    # safer error.
    excess = venue.ceil_step(fut_qty - spot_qty, fut_step)
    if excess <= 0:
        print("\n  nothing to reduce; leg 1 already matches leg 2\n")
        return 0

    order = {
        "tool": "futures_usds_newOrder",
        "symbol": st["symbol"],
        "side": "BUY",
        "type": "MARKET",
        "quantity": venue.fmt_qty(excess, fut_step),
        "reduceOnly": "true",
    }
    st["unwind_order"] = order
    save_state(st)
    log("unwind_plan", {"order": order, "fut": str(fut_qty), "spot": str(spot_qty)})

    print(f"\n  UNWIND - reduce leg 1 to match leg 2")
    print(f"  short {fut_qty}, hedged {spot_qty}, naked {fut_qty - spot_qty}")
    print(f"  buy back {excess} (rounded up to the futures step)\n")
    print(f"    {json.dumps(order)}\n")
    print(f"  then: python executor.py record-close --leg fut --response <json>\n")
    return 0


# --- closing ----------------------------------------------------------

def cmd_close(args):
    st = load_state()
    if st["phase"] not in ("hedged", "leg2_short"):
        sys.exit(f"cannot close from phase '{st['phase']}'")

    fut_qty = D(st["leg1_fill"]["executed_qty"])
    spot_qty = D(st["leg2_fill"]["executed_qty"])

    fut_close = {
        "tool": "futures_usds_newOrder",
        "symbol": st["symbol"], "side": "BUY", "type": "MARKET",
        "quantity": venue.fmt_qty(fut_qty, D(st["fut_step"])),
        "reduceOnly": "true",
    }
    # Selling back spot, the commission already came out in BNB on the way
    # in, so the full bought quantity is there to sell.
    spot_close = {
        "tool": "spot_newOrder",
        "symbol": st["symbol"], "side": "SELL", "type": "MARKET",
        "quantity": venue.fmt_qty(venue.floor_step(spot_qty, D(st["spot_step"])),
                                  D(st["spot_step"])),
    }
    st["close_orders"] = {"fut": fut_close, "spot": spot_close}
    st["phase"] = "closing"
    save_state(st)
    log("close_plan", {"orders": st["close_orders"]})

    print(f"\n  CLOSE both legs")
    print(f"    {json.dumps(fut_close)}")
    print(f"    {json.dumps(spot_close)}")
    print(f"\n  record each: python executor.py record-close --leg fut|spot "
          f"--response <json>\n")
    return 0


def cmd_record_close(args):
    st = load_state()
    resp = read_response(args.response)
    if args.leg == "fut":
        query = read_response(args.query) if args.query else None
        fill = parse_futures_fill(resp, query)
        require_price(fill, fill["order_id"], st["symbol"])
    else:
        fill = parse_spot_fill(resp)
    st.setdefault("close_fills", {})[args.leg] = fill
    st[f"close_{args.leg}_at"] = now()
    if len(st["close_fills"]) == 2:
        st["phase"] = "closed"
    save_state(st)
    log("close_fill", {"leg": args.leg, "fill": fill})

    print(f"\n  {args.leg} close FILLED  order {fill['order_id']}  "
          f"{fill['executed_qty']} @ {fill['avg_price']}")
    if st["phase"] == "closed":
        print(f"\n  both legs closed. python executor.py report\n")
    else:
        print(f"\n  one leg still open. Close the other now.\n")
    return 0


# --- reconciliation ---------------------------------------------------

def cmd_report(args):
    st = load_state()
    b = st["base"]
    print(f"\n=== {st['symbol']} {st['mode']} ===")
    print(f"  {st['note']}\n")

    l1, l2 = st.get("leg1_fill"), st.get("leg2_fill")
    if not l1:
        print("  no fills recorded yet\n")
        return 1

    print(f"  {'leg':<20}{'order id':>14}{'qty':>16}{'avg price':>14}{'USDT':>12}")
    print("  " + "-" * 74)
    print(f"  {'1 SELL perp':<20}{str(l1['order_id']):>14}"
          f"{l1['executed_qty']:>16}{px(l1['avg_price']):>14}{l1['quote_qty']:>12}")
    if l2:
        print(f"  {'2 BUY spot':<20}{str(l2['order_id']):>14}"
              f"{l2['executed_qty']:>16}{px(l2['avg_price']):>14}{l2['quote_qty']:>12}")
    for leg, f in (st.get("close_fills") or {}).items():
        label = "close BUY perp" if leg == "fut" else "close SELL spot"
        print(f"  {label:<20}{str(f['order_id']):>14}"
              f"{f['executed_qty']:>16}{px(f['avg_price']):>14}{f['quote_qty']:>12}")

    if st.get("residual"):
        r = st["residual"]
        print(f"\n  residual delta   "
              f"{venue.fmt_qty(D(r['base']), D(st['spot_step']))} {b} "
              f"= {float(D(r['usd'])):.6f} USDT")
        if r["bps_of_leg"]:
            print(f"                   {float(D(r['bps_of_leg'])):.2f} bps of the "
                  f"leg, net {r['direction']} {b}")
    if st.get("exposure_seconds") is not None:
        print(f"  exposure window  {st['exposure_seconds']}s one-sided between legs")

    # Realised: spot is a cash round trip, the perp is entry minus exit.
    closes = st.get("close_fills") or {}
    if l2 and "fut" in closes and "spot" in closes:
        fut_open_px = D(l1["avg_price"])
        fut_close_px = D(closes["fut"]["avg_price"])
        fut_qty = D(closes["fut"]["executed_qty"])
        fut_pnl = (fut_open_px - fut_close_px) * fut_qty     # short

        spot_pnl = D(closes["spot"]["quote_qty"]) - D(l2["quote_qty"])

        # Spot commission is real and in BNB; futures commission is not in
        # the order response, so it is DERIVED from the verified 5 bps rate
        # and labelled as such rather than quietly folded in.
        fut_notional = D(l1["quote_qty"]) + D(closes["fut"]["quote_qty"])
        fut_fee_derived = fut_notional * venue.FUT_TAKER

        # Spot commission settles in BNB, so it is not in the USDT arithmetic
        # above. Leaving it out would understate the cost, so it is converted
        # at the live BNBUSDT price and the price used is printed.
        bnb_fee = ZERO
        for f in (l2, closes.get("spot")):
            if f:
                bnb_fee += D((f.get("commissions") or {}).get("BNB", "0"))
        bnb_px = ZERO
        if bnb_fee > 0:
            try:
                bnb_px = D(venue.http_get(f"{venue.SAPI}/api/v3/ticker/price",
                                          {"symbol": "BNBUSDT"})["price"])
            except Exception:                        # noqa: BLE001
                bnb_px = ZERO
        bnb_fee_usd = bnb_fee * bnb_px

        gross = fut_pnl + spot_pnl
        net = gross - fut_fee_derived - bnb_fee_usd
        notional = D(l1["quote_qty"])

        print(f"\n  REALISED")
        print(f"    perp   {float(fut_pnl):+.6f} USDT  "
              f"(short {px(fut_open_px)} -> {px(fut_close_px)})")
        print(f"    spot   {float(spot_pnl):+.6f} USDT")
        print(f"    futures commission  -{float(fut_fee_derived):.6f} USDT "
              f"[DERIVED from the verified 5 bps rate, not from a fill]")
        if bnb_fee > 0:
            print(f"    spot commission     -{float(bnb_fee_usd):.6f} USDT "
                  f"({bnb_fee} BNB from the fills, converted at the live "
                  f"BNBUSDT price {px(bnb_px)} at report time)")
        print(f"    net    {float(net):+.6f} USDT = "
              f"{float(net / notional * BPS):+.2f} bps of one leg")

        pred = D(st["predicted"]["total_bps"])
        realised_cost_bps = -net / notional * BPS
        print(f"\n  PREDICTED vs REALISED")
        print(f"    predicted cost  {float(pred):.2f} bps "
              f"({float(D(st['predicted']['fees_bps'])):.2f} fees + "
              f"{float(D(st['predicted']['spread_bps'])):.2f} spread)")
        print(f"    realised cost   {float(realised_cost_bps):.2f} bps")
        print(f"    difference      {float(realised_cost_bps - pred):+.2f} bps")
        log("report", {"realised_bps": str(realised_cost_bps),
                       "predicted_bps": str(pred),
                       "residual": st.get("residual")})
    else:
        print(f"\n  position still open; realised cost needs both closes\n")
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan");      p.set_defaults(fn=cmd_plan)
    p.add_argument("--target", type=Decimal, default=Decimal("5"))
    p.add_argument("--symbol")

    p = sub.add_parser("preflight"); p.set_defaults(fn=cmd_preflight)
    p.add_argument("--target", type=Decimal, default=Decimal("5"))
    p.add_argument("--symbol")

    p = sub.add_parser("record-leg1"); p.set_defaults(fn=cmd_record_leg1)
    p.add_argument("--response", required=True)
    p.add_argument("--query", help="futures_usds_queryOrder JSON, which "
                                   "carries the avgPrice newOrder omits")

    p = sub.add_parser("record-leg2"); p.set_defaults(fn=cmd_record_leg2)
    p.add_argument("--response", required=True)

    p = sub.add_parser("unwind");    p.set_defaults(fn=cmd_unwind)
    p = sub.add_parser("close");     p.set_defaults(fn=cmd_close)

    p = sub.add_parser("record-close"); p.set_defaults(fn=cmd_record_close)
    p.add_argument("--leg", choices=["fut", "spot"], required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--query", help="futures_usds_queryOrder JSON for a "
                                   "futures leg")

    p = sub.add_parser("report");    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
