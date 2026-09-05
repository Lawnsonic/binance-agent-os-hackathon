"""
costcheck: an MCP server that prices a proposed trade before it is placed.

    claude mcp add costcheck -- python cost_mcp.py

One tool, stdio transport, no port, no OAuth, no credentials. It reads public
order books and does arithmetic. It cannot place an order, cannot move funds,
and holds no keys, so the worst a compromised copy of it can do is quote you
a bad number, which is also the reason it is safe to leave connected.

WHAT IT IS FOR
--------------
An agent connected to Binance Agent OS can place real orders. It reasons in
sentences, and the loss lives in basis points. This gives it the number.

The tool returns a decision object rather than prose, because prose is the
failure mode: a paragraph explaining that a trade is expensive is something a
model can talk itself past, whereas `"decision": "REJECT", "shortfall_bps":
30.29` is not. The arithmetic arrives in the context as a value, not as
something the model was supposed to remember or derive.

WHAT IT IS NOT
--------------
It is not a security boundary, and pretending otherwise would be the most
dangerous claim in this repository. The agent still holds `spot_newOrder`. It
can call this tool, read REJECT, and place the order anyway; nothing here can
stop it. This is an advisory layer enforced by policy, and the policy is a
line in CLAUDE.md that a model can ignore.

Making it non-bypassable means the agent never holding the raw order tool at
all: it calls a gateway, the gateway prices the trade, and only the gateway
can reach Binance. That is a different piece of software from this one and it
is not built here. What is built here is the arithmetic such a gateway would
have to run, which is the part that has to be right first.

WHY EVERY FAILURE RETURNS A REFUSAL
-----------------------------------
An exception reaching the agent is worse than useless, because a model that
asked for a cost and got an error will often shrug and continue. So every
path out of this file is a decision object, and every path that could not
finish the arithmetic is a REJECT. Fail closed. The one thing this tool must
never do is stay silent and let the order through.
"""

import sys
import traceback
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer

import cost_oracle

# stdio transport puts the JSON-RPC stream on stdout, so anything else printed
# there corrupts the protocol. Nothing in this project prints on import, but a
# future dependency might, so stdout is pointed at stderr for the duration of
# the import-time work and restored for the server itself.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

server = MCPServer(
    name="costcheck",
    version="1.0.0",
    instructions=(
        "Pre-trade cost oracle for Binance delta-neutral funding carry. Call "
        "evaluate_trade before placing any order that opens or closes a "
        "hedge. It returns a structured verdict with the full cost breakdown "
        "measured against live order books at the requested size. Treat "
        "decision=REJECT as binding and do not place the order. A decision is "
        "only valid until its expires_at timestamp, which is seconds away by "
        "design: if it has expired, call again rather than acting on it."
    ),
)


def _refusal(symbol, reason, detail):
    """A decision object for the cases where there is no arithmetic to show."""
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    expiry = (now + timedelta(seconds=cost_oracle.TTL_SECONDS)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return {
        "schema_version": cost_oracle.SCHEMA_VERSION,
        "decision": "REJECT",
        "reason": reason,
        "detail": detail,
        "symbol": symbol,
        "measured_at": stamp,
        "expires_at": expiry,
        "ttl_seconds": cost_oracle.TTL_SECONDS,
        "max_notional_usdt": 0.0,
    }


@server.tool(
    name="evaluate_trade",
    title="Price a proposed trade against live books",
    description=(
        "Price a delta-neutral funding carry (long spot, short USD-M perp) on "
        "one symbol, at the size actually being traded, against order books "
        "read at the moment of the call. Returns a decision object: APPROVE "
        "or REJECT, the cost stack in basis points (verified taker fees, "
        "cross-venue entry basis measured at size, exit spread, lot-step "
        "residual), the expected funding against it, and an expiry a few "
        "seconds out. Call this before any order that opens or closes a "
        "hedge. REJECT is binding."
    ),
)
def evaluate_trade(
    symbol: str,
    notional_usdt: float = 5.0,
    hold_periods: int = 3,
    min_edge_bps: float = 10.0,
) -> dict:
    """
    Args:
        symbol: Binance symbol listed on both spot and USD-M, e.g. STRKUSDT.
        notional_usdt: Intended size of one leg. Sized up to the venue
            minimum if it is below it, and the object says when that happened,
            because a quote for a size the venue will not accept is not a
            quote.
        hold_periods: 8h funding settlements the carry is underwritten over.
        min_edge_bps: Profit required above all costs before this returns
            APPROVE.
    """
    try:
        return cost_oracle.evaluate(
            symbol=symbol,
            notional_usdt=notional_usdt,
            hold_periods=hold_periods,
            min_edge_bps=min_edge_bps,
        )
    except Exception as exc:                          # noqa: BLE001
        # Deliberately broad. A network timeout, a delisted symbol and a bug
        # in this file all mean the same thing to the caller: the cost of this
        # trade is currently unknown, and an unknown cost is not a green light.
        traceback.print_exc(file=sys.stderr)
        return _refusal(
            symbol.upper().strip() if isinstance(symbol, str) else str(symbol),
            "pricing_failed",
            f"{type(exc).__name__}: {exc}. The cost of this trade could not be "
            f"established, so it is refused. This is not a signal that the "
            f"trade is bad, only that it is unpriced.",
        )


if __name__ == "__main__":
    sys.stdout = _real_stdout
    server.run("stdio")
