"""
Shared venue layer: exchange metadata, prices, and lot-step arithmetic.

Everything the symbol selector and the executor must agree on lives here.
If they round quantities differently the hedge is wrong in a way nobody
notices until the residual turns up in the P&L, so there is exactly one
implementation of each rule.

Public REST only. No auth. MCP is for execution and account state.

Two failure modes this module exists to prevent:

  1. Float lot-step rounding. 5.0/832.1 truncated to a 0.00001 step in
     binary floating point lands on 0.00600999..., which the API either
     rejects or silently re-rounds. All quantity maths is Decimal.
  2. Re-downloading a 2 MB exchangeInfo mid-hedge. Filters are cached on
     disk with a TTL so the executor pays the network cost before it has
     an open position, not between the two legs.
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

import requests

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"

TIMEOUT = 15
RETRIES = 3

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_TTL = 3600          # exchangeInfo filters change on the order of weeks

# --- Cost model -------------------------------------------------------
# Mirrors scanner.py. Verified against this account fee tier, not assumed.
SPOT_TAKER = Decimal("0.0010")     # 10 bps, charged in the BASE asset on a BUY
FUT_TAKER = Decimal("0.0005")      # 5 bps, charged in USDT


# --- HTTP -------------------------------------------------------------

def http_get(url, params=None, timeout=TIMEOUT, retries=RETRIES):
    """GET with backoff. Honours Retry-After on 429/418 instead of hammering."""
    delay = 0.5
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 418):
                wait = float(r.headers.get("Retry-After", delay))
                time.sleep(wait)
                delay *= 2
                last = RuntimeError(f"{r.status_code} rate limited: {url}")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001 - retry anything
            last = e
            if attempt == retries - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise last


def _cached(name, fetch, ttl=CACHE_TTL, refresh=False):
    """Disk-cache a JSON payload. Latency control, not correctness."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if not refresh and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh), age
            except (OSError, json.JSONDecodeError):
                pass                                # fall through and refetch
    data = fetch()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data, 0.0


# --- Lot-step arithmetic ----------------------------------------------

def floor_step(qty: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of step that is <= qty. A step of 0 means unfiltered."""
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_CEILING) * step


def fmt_qty(qty: Decimal, step: Decimal) -> str:
    """Quantity as the API wants it: exactly as many decimals as the step."""
    if step <= 0:
        return format(qty.normalize(), "f")
    return format(qty.quantize(step), "f")


# --- Symbol specs -----------------------------------------------------

@dataclass
class SymbolSpec:
    symbol: str
    venue: str            # "spot" | "futures"
    base: str
    quote: str
    step: Decimal         # effective quantity step for a MARKET order
    min_qty: Decimal      # effective minimum quantity for a MARKET order
    min_notional: Decimal
    tick: Decimal

    def as_json(self):
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _filters(sym):
    return {f["filterType"]: f for f in sym.get("filters", [])}


def spot_specs(refresh=False):
    """USDT spot pairs that are actually tradable, keyed by symbol."""
    data, age = _cached(
        "spot_exchangeInfo.json",
        lambda: http_get(f"{SAPI}/api/v3/exchangeInfo"),
        refresh=refresh,
    )
    out = {}
    for s in data["symbols"]:
        if s["status"] != "TRADING" or s["quoteAsset"] != "USDT":
            continue
        if not s.get("isSpotTradingAllowed", False):
            continue
        f = _filters(s)
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        # MARKET_LOT_SIZE of 0 means no market-specific limit, so the binding
        # constraint is whichever of the two filters is actually set.
        step = max(Decimal(lot.get("stepSize", "0")), Decimal(mlot.get("stepSize", "0")))
        min_qty = max(Decimal(lot.get("minQty", "0")), Decimal(mlot.get("minQty", "0")))
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        # applyMinToMarket false means the floor does not bind a market order.
        if notional.get("applyMinToMarket", True):
            min_notional = Decimal(notional.get("minNotional", "0"))
        else:
            min_notional = Decimal("0")
        out[s["symbol"]] = SymbolSpec(
            symbol=s["symbol"], venue="spot",
            base=s["baseAsset"], quote=s["quoteAsset"],
            step=step, min_qty=min_qty, min_notional=min_notional,
            tick=Decimal(f.get("PRICE_FILTER", {}).get("tickSize", "0")),
        )
    return out, age


def futures_specs(refresh=False):
    """USD-M perpetuals, USDT-margined, keyed by symbol."""
    data, age = _cached(
        "futures_exchangeInfo.json",
        lambda: http_get(f"{FAPI}/fapi/v1/exchangeInfo"),
        refresh=refresh,
    )
    out = {}
    for s in data["symbols"]:
        if s["status"] != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        if s["quoteAsset"] != "USDT" or s.get("marginAsset") != "USDT":
            continue
        f = _filters(s)
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        step = max(Decimal(lot.get("stepSize", "0")), Decimal(mlot.get("stepSize", "0")))
        min_qty = max(Decimal(lot.get("minQty", "0")), Decimal(mlot.get("minQty", "0")))
        out[s["symbol"]] = SymbolSpec(
            symbol=s["symbol"], venue="futures",
            base=s["baseAsset"], quote=s["quoteAsset"],
            step=step, min_qty=min_qty,
            min_notional=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "0")),
            tick=Decimal(f.get("PRICE_FILTER", {}).get("tickSize", "0")),
        )
    return out, age


# --- Top of book ------------------------------------------------------

@dataclass
class Book:
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self):
        m = self.mid
        if m <= 0:
            return Decimal("9999")
        return (self.ask - self.bid) / m * 10_000


def _books(rows):
    out = {}
    for r in rows:
        try:
            b = Book(Decimal(r["bidPrice"]), Decimal(r["bidQty"]),
                     Decimal(r["askPrice"]), Decimal(r["askQty"]))
        except Exception:                            # noqa: BLE001
            continue
        if b.bid <= 0 or b.ask <= 0:
            continue                                 # no two-sided market
        out[r["symbol"]] = b
    return out


def spot_books():
    """Best bid/ask and top-of-book size for every spot symbol. One call."""
    return _books(http_get(f"{SAPI}/api/v3/ticker/bookTicker"))


def futures_books():
    """Same for USD-M perpetuals. One call."""
    rows = http_get(f"{FAPI}/fapi/v1/ticker/bookTicker")
    return _books(rows if isinstance(rows, list) else [rows])
