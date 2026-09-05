# The arithmetic layer that says no

**A pre-trade cost oracle for Binance Agent OS.**

A language model can decide what trade it wants to make. It should not be the
thing that decides whether that trade is economically valid.

Those are two different jobs and they want two different machines. Choosing
what to trade is judgement about a market, and a model is good at it. Deciding
whether the trade survives its own costs is arithmetic against live order
books, at the size actually being traded, in basis points, and a model is not
good at that at all. It reasons in sentences while the loss lives three
decimal places down.

This repository is the second job, done deterministically in Python and handed
back to the agent as a number rather than as advice. It ships as an MCP server:
the agent calls `evaluate_trade` before it places an order and gets a verdict
with the full arithmetic attached.

---

## The failure mode

A model sees a 30 bps gross funding opportunity. It opens the hedge, closes
it, and reports a successful trade. It is telling the truth: the orders
filled, the position closed, nothing errored.

It also lost money, because 44 bps of cost never entered its reasoning.

Run that a thousand times and the model's own log shows a thousand successes
while the account bleeds out. **The agent is not wrong about what it did. It
is wrong about what it earned.**

This is not wash trading, which implies intent to fake volume. This is
simpler and worse: sincere, competent execution against an incomplete cost
model. Nothing in the model's context contained the number it needed.

Binance Agent OS now lets any language model place real orders. Ask one to
farm the highest funding rate on the board and it will do so, correctly and
confidently, and lose about 44 bps per cycle.

## Then the cost oracle was wrong too

This is the part worth reading, and it is why the project ended up somewhere
more interesting than it started.

The scanner refused every signal on the board for nineteen hours. To prove the
execution path was real rather than a program that only knows how to print
"no", a minimum-size hedge was opened and closed on STRKUSDT on 2026-09-05 at
06:33 UTC. It was declared in advance as a mechanism test, not a trade taken
on merit.

It cost **55.50 bps** against a predicted **31.77**, and it did not just beat
the prediction. It beat **the scanner's own 44.32 bps refusal threshold, by
11.18 bps**, and 44.32 was already the twice-corrected, deliberately
conservative figure.

Fees behaved exactly as verified: 25.00 bps, to the cent. The entire overshoot
was the cross-venue entry basis, and the log holds four different values for
that one quantity inside a single minute:

```
 6.80 bps   measured by select_symbol.py on the live book
13.55 bps   recorded by the executor's own plan, seconds later
19.32 bps   carried in the FRICTION constant
30.50 bps   actually paid on the fill
```

**And the error was not only in the constant. It was in the model.** The
executor predicted the price cost of a round trip as the spreads it crosses,
6.77 bps. The fills say the round trip costs the basis you enter at plus the
basis you exit at, which are only the same number when the two venues are
equally dislocated at the open and at the close. They were not: nine ticks
apart going in, level coming out.

```
open    spot BUY  0.02962    perp SELL 0.02953    basis  +30.43 bps
close   spot SELL 0.02954    perp BUY  0.02954    basis    0.00 bps
                                                  ----------------
entry basis + exit basis                                  30.43 bps
realised price term, from the reconciliation              30.50 bps
```

Opening a hedge into a wide positive basis means being long the expensive
venue and short the cheap one. If the dislocation holds you give the basis
back on the way out and pay only spreads. If it converges you eat all of it,
and convergence is the direction a basis tends to move. The old model priced
the lucky case.

`cost_oracle.py` is that correction: it prices entry basis plus exit spread,
measured at the size actually being traded, at the moment of the call. Against
the fills above it returns 30.43 where the spread model returned 6.77.

So the honest summary of this project is not "we built a guardrail." It is:
**we built a guardrail, pointed it at ourselves, and found our own arithmetic
was the next thing wrong.** The refusals stand harder for it, not softer.
Nothing on the board came within 30 bps of clearing, so a threshold that was
11 bps too low changed no decision in this window. But it would have, on a
board where something was close.

## The obvious objection

*Pre-trade cost checks are forty years old. Every professional system has
them. And if the concern is fees, just put the fee schedule in the prompt.*

The answer is in this repository's own logs.

**Every cost assumption made in prose during this build was wrong when
measured.** Four assumptions, four corrections, every one found by
measurement and none by reasoning. This was a careful build, with explicit
written instructions to be careful.

| # | Assumed | Measured | How it was verified |
|---|---|---|---|
| 1 | Spot taker fee **10 bps** | **7.5 bps** | `spot.accountCommission` returned `discount: 0.75`, which the docs gloss ambiguously as "reduced by this rate" (implying 2.5 bps). Two settled fills resolved it arithmetically and agreed: `0.00000450/0.006` and `0.00000675/0.009`, both `= 0.00075`. `commissionAsset` was BNB on both. |
| 2 | Execution friction **5 bps** | **19.32 bps**, then **30.5 bps** | Wrong twice, both times in the same direction. First: cross-venue entry basis measured by `select_symbol.py` on STRKUSDT at 2026-09-04 17:40 UTC: 19.32 bps, off by a factor of nearly four. Then the paired run of 2026-09-05 06:33 UTC paid **30.5 bps** on the fill (orders 5102989720 / 1521379862), against **6.8 bps** that `select_symbol.py` had measured on the same symbol under a minute earlier, **13.55 bps** that the executor wrote into its own plan record, and the 19.32 carried in the constant. |
| 3 | Lot-step truncation is the **dominant** error at small size | **6 of 356 pairs** | `select_symbol.py` compared both `exchangeInfo` endpoints across every hedgeable pair. Only 6 have a spot step coarser than their futures step; for the rest cross-venue truncation is exactly zero. |
| 4 | Futures `newOrder` returns the fill price | **It does not** | Preflight order `6578993465` came back `FILLED` with `executedQty` but no `avgPrice` and no `cumQuote`, even at `newOrderRespType=RESULT`. `futures_usds_queryOrder` on the same id has both. Unhandled, this records a fill price of zero. |

Assumption 2 is the one that kills the prompt-engineering answer. **The entry
basis is not in any fee schedule.** It is the live gap between two order
books. It changes by the second and it varies per symbol. The only way to
know it is to measure it at the moment of trading, which is exactly what a
language model cannot do by reasoning and exactly what this layer does.

**Assumption 2 is also the only one that was wrong twice**, and the second
time it was wrong against a number this project had itself measured from a
live book under a minute before the order. Four values for one quantity inside
that minute: 6.8 bps measured by the selector, 13.55 recorded by the executor's
own plan, 19.32 carried in the constant, 30.5 paid on the fill. If
measuring it carefully and then acting a minute later is not good enough,
then reasoning about it in sentences is not a near miss. It is not the same
kind of activity. That is the argument, and it is made at this project's own
expense rather than at a hypothetical agent's.

Assumption 4 is the one that kills "just be careful." It is not an
arithmetic error at all. It is an undocumented gap in an API response that
would have silently poisoned every downstream number.

## What this actually is

Not an arbitrage bot. A **pre-trade cost oracle**, with delta-neutral funding
capture as its reference implementation.

The reusable part takes a proposed trade, prices the complete round trip
against live books, and returns a verdict with the arithmetic attached. The
funding scanner is one caller of it. `cost_mcp.py` is the other, and that one
any Agent OS agent can reach.

## The MCP server

`cost_mcp.py` is a local MCP server. It speaks the Model Context Protocol over
stdio and exposes exactly one tool, `evaluate_trade`, which prices a proposed
delta-neutral hedge against live order books and returns a structured verdict.
Any MCP-capable client can call it. Nothing in it is specific to one agent
harness.

### Contract

```
command       python cost_mcp.py
transport     stdio, JSON-RPC on stdin and stdout
port          none
credentials   none, no API key and no OAuth
network       outbound HTTPS to public Binance market data endpoints only
tools         evaluate_trade
```

It reads public order books and does arithmetic. It cannot place an order,
cannot move funds and holds no keys, so the worst a compromised copy of it can
do is quote a bad number. That is also why it is safe to leave connected.

Because stdout carries the protocol stream, the server writes its diagnostics
to stderr and redirects stdout during import, so nothing a dependency prints
can corrupt the transport.

### Registering it

Most clients accept this block, in their own config file:

```json
{"mcpServers": {"costcheck": {"command": "/abs/path/python", "args": ["/abs/path/cost_mcp.py"]}}}
```

Both paths must be absolute. The client launches the process itself, and its
working directory is not this repository. `/abs/path/python` is the interpreter
of the environment that has `requirements.txt` installed, which on Windows is
usually `...\.venv\Scripts\python.exe`. `/abs/path/cost_mcp.py` is this file's
full path.

Clients that read that shape:

| Client | Where the block goes |
| --- | --- |
| Claude Code | `.mcp.json` in the project root, or user scope via `claude mcp add` |
| Claude Desktop | `claude_desktop_config.json`, reachable from Settings, Developer, Edit Config |
| VS Code | `.vscode/mcp.json`, the same `command` and `args` pair under its own top-level key |
| Codex | `~/.codex/config.toml`, the same `command` and `args` pair expressed as TOML |
| Any MCP client | Anything that can spawn a stdio server takes the same two fields |

Claude Code has a convenience path that writes the entry for you:

```bash
claude mcp add costcheck -- /abs/path/python /abs/path/cost_mcp.py
/mcp                                              # confirm it connected
```

That is one way in, not the way in. The server does not know or care which
client spawned it.

### The tool

```
evaluate_trade(symbol, notional_usdt=5.0, hold_periods=3, min_edge_bps=10.0)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `symbol` | string | required | A symbol listed on both USDT spot and USD-M futures, for example `STRKUSDT`. Case is normalised. |
| `notional_usdt` | float | `5.0` | Intended size of one leg. Sized up to the venue minimum if it is below it, and the object says when that happened, because a quote for a size the venue will not accept is not a quote. |
| `hold_periods` | int | `3` | Number of 8h funding settlements the carry is underwritten over. |
| `min_edge_bps` | float | `10.0` | Profit required above all costs and buffers before the tool returns APPROVE. |

It returns a decision object rather than prose, and the shape is the argument.
Prose is the failure mode: a paragraph explaining that a trade looks expensive
is something a model can talk itself past. `"decision": "REJECT",
"shortfall_bps": 74.1` is not.

A complete response, measured live at the timestamp it carries:

```json
{
  "schema_version": "1.0.0",
  "symbol": "HOTUSDT",
  "strategy": "funding_carry_delta_neutral",
  "measured_at": "2026-09-05T17:45:27Z",
  "expires_at": "2026-09-05T17:45:37Z",
  "ttl_seconds": 10,
  "size": {
    "requested_notional_usdt": 5.0,
    "futures_qty": "13242",
    "spot_qty": "13242.00000000",
    "priced_notional_usdt": 5.0002,
    "scaled_to_venue_minimum": true,
    "lot_residual_base": "0.00000000",
    "lot_residual_usdt": 0.0,
    "depth_cover_x": 21016.2
  },
  "cost_bps": {
    "fees": 25.0, "entry_basis": 10.59, "exit_spread": 14.56,
    "lot_residual": 0.0, "total": 50.15
  },
  "edge_bps": {
    "funding_per_settlement": 1.0, "hold_periods": 3, "gross_funding": 3.0,
    "net": -47.15, "risk_adjusted": -64.1, "min_edge_required": 10.0
  },
  "uncertainty_bps": {
    "depth_impact": 0.0, "calibration_gap": 16.95, "calibration_n": 1,
    "total_buffer": 16.95
  },
  "evidence": {
    "spot_ask_vwap": "0.00037800", "futures_bid_vwap": "0.0003776",
    "spot_bid_vwap": "0.00037700", "futures_ask_vwap": "0.0003777",
    "entry_basis_measured_bps": 10.59,
    "entry_basis_top_of_book_bps": 10.59,
    "hours_to_next_funding": 6.24,
    "funding_prints_bps": ["1.00000000", "2.62590000", "1.00000000"],
    "funding_sign_persistent": true,
    "book_depth_levels": 100,
    "quote_latency_ms": 3452,
    "fee_basis": "spot 7.5 bps and futures 5 bps, verified 2026-09-04 against settled fills; see venue.py"
  },
  "decision": "REJECT",
  "reason": "insufficient_edge",
  "max_notional_usdt": 0.0,
  "shortfall_bps": 74.1
}
```

### Every field

**Envelope**

- `schema_version`: version of this object's shape, bumped if a field changes meaning.
- `symbol`: the symbol as the oracle resolved it.
- `strategy`: what was priced. Currently always `funding_carry_delta_neutral`, long spot against short USD-M perp.
- `measured_at`: UTC instant the books were read.
- `expires_at`: `measured_at` plus `ttl_seconds`. After this the decision is void.
- `ttl_seconds`: lifetime of the decision, currently 10.
- `decision`: `APPROVE` or `REJECT`. Nothing else.
- `reason`: machine-readable verdict code. One of `edge_clears_cost`, `insufficient_edge`, `funding_not_positive`, `funding_sign_not_persistent`, `too_close_to_settlement`, `no_futures_market`, `no_spot_market`, `base_asset_mismatch`, `no_two_sided_market`, `below_min_notional`, `insufficient_depth`, `pricing_failed`.
- `detail`: present on refusals that have something to add in words. Never the only carrier of a number.
- `max_notional_usdt`: the size this decision authorises, in USDT, on this symbol. `0.0` on every REJECT.
- `shortfall_bps`: REJECT only. How far the risk-adjusted edge fell below `min_edge_bps`, which is the distance to a yes.
- `authorization`: APPROVE only, described below.

**`size`**, what was actually priced

- `requested_notional_usdt`: what the caller asked for.
- `futures_qty`, `spot_qty`: leg quantities after each venue's lot step.
- `priced_notional_usdt`: the notional the quote actually covers, which is the number that matters if the request was scaled.
- `scaled_to_venue_minimum`: true when the request was below the venue minimum and was raised to it.
- `lot_residual_base`, `lot_residual_usdt`: the unhedgeable remainder created by the two venues having different lot steps.
- `depth_cover_x`: how many times the visible book covers this order. A thin book shows up here before it shows up in a fill.

**`cost_bps`**, the charge stack, in basis points of notional

- `fees`: four taker fills, spot and futures, both directions. Verified against settled fills, not read off a schedule.
- `entry_basis`: the cross-venue gap paid on the way in, measured by walking the depth ladder for the actual quantity rather than reading top of book. Top of book is a price for the first slice of an order, not for the order.
- `exit_spread`: the round trip out, priced the same way.
- `lot_residual`: cost of the unhedged remainder above.
- `total`: the sum, and the number an edge has to beat.

A favourable dislocation, meaning a negative entry basis, is reported in
`evidence` at its measured value but floored to zero here. A dislocation in
your favour that lasts a second should not be allowed to manufacture a trade.

**`edge_bps`**, what the trade earns

- `funding_per_settlement`: current funding rate, in bps per 8h.
- `hold_periods`: settlements underwritten, echoed from the call.
- `gross_funding`: rate times periods, before any cost.
- `net`: `gross_funding` minus `cost_bps.total`.
- `risk_adjusted`: `net` minus `uncertainty_bps.total_buffer`. This is the number compared against `min_edge_required`.
- `min_edge_required`: echoed from `min_edge_bps`.

**`uncertainty_bps`**, what the model knows it does not know

- `depth_impact`: how much worse the VWAP at this size is than top of book. Measured, not modelled.
- `calibration_gap`: how far this model has under-charged on executions that actually settled, read back out of `trades.jsonl`.
- `calibration_n`: how many settled executions that average is built on. It is published because at `n: 1` the gap is an anecdote rather than a distribution. No standard deviation is quoted, because there is not one to quote yet. It grows with the log.
- `total_buffer`: the two above, summed, and subtracted from `net`.

**`evidence`**, the inputs, so the verdict can be recomputed by hand

VWAPs for all four legs, the measured entry basis before flooring, the same
basis at top of book for comparison, hours to next funding, the recent funding
prints and whether their sign held, book depth read, quote latency, and the
provenance of the fee numbers.

**`authorization`**, on APPROVE only

Names the symbol, the two legs in execution order, the quantity, a
`max_entry_basis_bps` ceiling and the same expiry. An approval is not
permission to trade. It is permission to make this one economic decision, at
this size, for the next few seconds.

### Two rules the caller has to honour

**REJECT is binding.** It is not advice to weigh against other considerations.
That includes `reason: "pricing_failed"`, which means the cost is unknown
rather than acceptable, and an unknown cost is not a green light. Every path
out of the tool is a decision object, and every path that could not finish the
arithmetic returns REJECT. A network timeout, a delisted symbol and a bug in
the file all mean the same thing to the caller. Fail closed.

**Decisions expire after 10 seconds.** Not minutes. That is not caution for
its own sake, it is the direct lesson of the paired run below: this project
measured a basis, acted on it under a minute later, and paid 2.25x what it
measured. The cross-venue basis on STRKUSDT moved from 13.55 to 30.50 bps
inside that minute. A cost quote with a long life is a lie about how fast the
quantity moves. If `expires_at` has passed, call again rather than acting on
what you have.

### The policy is the caller's job

Neither rule is enforced by this server, and implying otherwise would be the
most dangerous claim in this repository. The server has no idea whether the
client that called it went on to place the order. A client holding raw order
tools can read REJECT and trade anyway.

In this repository the rule lives in `CLAUDE.md`, which is Claude Code's
convention for standing instructions. That file is not portable. A different
client needs the equivalent text wherever it reads instructions: `AGENTS.md`
for Codex, the system prompt or custom instructions for Claude Desktop, the
rules or workspace instructions file for VS Code. The wording matters less
than the three clauses: call `evaluate_trade` before any order that opens or
closes a position, treat REJECT as binding, and re-call rather than act on an
expired decision.

Making the check unbypassable requires a gateway that holds the Binance
credentials and refuses to forward an unpriced order. That is a different
piece of software and it is not built here. What is built here is the
arithmetic such a gateway would have to run, which is the part that has to be
right first. See Limitations.

## The cost model

Every figure below came from a live call. Nothing is inherited from a fee
schedule or estimated in prose.

```
spot taker         7.50 bps   x2   verified, BNB discount applied, paid in BNB
futures taker      5.00 bps   x2   USD-M
                  -----------------
round trip        25.00 bps        four taker fills
entry basis       19.32 bps        measured cross-venue, spot ask vs futures bid
                  -----------------
cost to beat      44.32 bps
```

Against that, the best funding rate seen anywhere on the board across the
observation window was **5.265 bps per 8h settlement** (HOTUSDT, at
2026-09-05 10:57 UTC), which needs **2.8 days** of holding to break even and
falls **28.5 bps short** over the three settlements the scanner is willing to
underwrite.

That is the best print out of an entire board, over nineteen hours. It is not
close.

## The result

At the time of writing, `refusals.jsonl` holds:

```
  window            2026-09-04 15:48 to 2026-09-05 11:23 UTC (19.6h)
  scans             69
  pair evaluations  25,185
  refused           69
  traded            0
```

Every opportunity on the board was refused. Not one cleared cost.

The log records the cost model with every single line, so a mid-run change to
a constant appears as a visible discontinuity in the record rather than
silently rewriting history. One such discontinuity exists, at
`2026-09-04T19:19:40Z`, where cost-to-beat moves 35.00 → 44.32 bps as
corrections 1 and 2 landed together.

`python report_refusals.py` prints the aggregate. `python build_report.py`
bakes both logs into a standalone `report.html` with no server and no CDN.

### On "it refuses everything, so it does nothing"

A brake that never engages is not broken. You have not driven down a hill.

The refusal is only meaningful if the same code executes when something does
clear, which is why the executor exists and why it has been run against the
live venue. The mechanism test proves the path is real. What separates this
from a program that merely prints "no" is that it is a program that
*decides*.

## Architecture

**This is not a headless bot, and the split matters.**

All sizing and cost arithmetic lives in deterministic, tested Python. The
agent does not compute quantities, prices, or residuals. It executes against
numbers the code produced, and feeds the raw API responses back so the code
can reconcile them.

Market data is **public REST only**. No auth, no MCP, no round-tripping
numeric data through a language model. MCP is used exclusively for execution
and account state.

| File | Role |
|---|---|
| `venue.py` | Shared venue layer. Decimal lot-step arithmetic, `exchangeInfo` filter parsing, disk-cached filters, top-of-book. Importable, no CLI dependency. |
| `cost_oracle.py` | The cost engine. Prices a proposed hedge against live depth at the traded size and returns a decision object. Importable, no MCP dependency. |
| `cost_mcp.py` | The `costcheck` MCP server. One tool, stdio, no credentials. Wraps `cost_oracle.py` and fails closed. |
| `scanner.py` | Signal engine. Scans the USD-M board for funding that clears the full cost stack. |
| `diagnose.py` | Funnel and distribution visibility. Proves the pipeline is not silently dropping everything. |
| `select_symbol.py` | Ranks every hedgeable pair by the residual a minimum-size hedge would leave, from live filters and live books. |
| `executor.py` | Two-legged execution state machine with fill-driven sizing and reconciliation. |
| `refusal_log.py` | Runs the scanner on a timer, appends every evaluation to `refusals.jsonl`. |
| `report_refusals.py` | Aggregate over the refusal log. |
| `build_report.py` | Bakes both logs into a standalone `report.html`. |

Two failure modes `venue.py` exists to prevent: float lot-step rounding
(`5.0/832.1` truncated to a `0.00001` step in binary floating point lands on
`0.00600999...`, which the API rejects or silently re-rounds, so all quantity
maths is `Decimal`), and re-downloading a 2 MB `exchangeInfo` mid-hedge.

### Why the executor is a state machine

Orders route through the Binance MCP server, authenticated by OAuth in the
agent session. Python cannot call it, and there are no API keys on disk. So
`executor.py` holds the state and does the arithmetic; the agent places each
order and feeds the response back.

That costs something real, and the log says so instead of glossing it:
between leg one filling and leg two filling there is an agent round trip, and
for the length of it the position is one-sided. `exposure_seconds` in the
report is that window, timed off the two fills.

The ordering rule is non-negotiable and is why this is a state machine rather
than one function:

1. **Futures leg first.** Read the *actual filled quantity* from the response.
2. **Spot leg sized from that fill**, truncated to spot's own `stepSize`,
   never from the figure both legs were planned against. A market order can
   fill short, and sizing leg two off the intention is how you end up with a
   hedge that is not a hedge.
3. **Log the residual.** In base units and USD. Knowing its size is the
   point; pretending it is zero is the failure.
4. **Partial fill unwinds immediately.** If leg two fails or fills short, leg
   one is reduced to match rather than leaving directional exposure open.

Verified by feeding a deliberately short fill: planned 182.7, filled 182.3,
leg two came out 182.30, not 182.70. A short leg two of 181.50 correctly
flagged 0.8 FLOW (43.89 bps) naked and produced a `reduceOnly` buy-back.

## The mechanism test

Capital is roughly **$15**, which constrains everything: ~$5 per leg
minimum, two legs, 1x on futures, no leverage.

Symbol selection for the test is by **execution cost and lot-step
granularity, not by funding rate**. No signal cleared, so there is no funding
trade to pick. `select_symbol.py` ranks the board and execution is
restricted to the cheapest of a pre-agreed cluster at run time.

**Preflight (executed live).** One unpaired minimum-size futures order, fired
deliberately before any paired run, so that a permission or response edge
case could not be discovered with one leg already open. It earned its place
immediately by surfacing correction 4.

```
open    FLOWUSDT SELL 182.8 MARKET 1x   order 6578993465   FILLED
        avgPrice 0.02737  cumQuote 5.003236  (via queryOrder, not newOrder)
close   FLOWUSDT BUY  182.8 reduceOnly  order 6578993477   FILLED
        positionInformationV2 -> positionAmt 0.0
```

**Paired run (executed live).** Both legs on STRKUSDT, 1x, minimum size,
2026-09-05 06:33 UTC. Futures first; the spot leg sized from leg one's actual
`executedQty`, not from the figure both legs were planned against.

```
open   leg 1  SELL 169.4 STRK perp   order 5102989720   @ 0.02953   5.002382
       leg 2  BUY  169.4 STRK spot   order 1521379862   @ 0.02962   5.017628
close  leg 1  BUY  169.4 STRK perp   order 5102992762   @ 0.02954   5.004076
       leg 2  SELL 169.4 STRK spot   order 1521380555   @ 0.02954   5.004076

residual   0.00000000 STRK = 0.000000 USDT, 0.00 bps, net flat
exposure   21.4s one-sided between leg one and leg two
```

Leg one filled its full size, so truncating leg two against the spot step was
a no-op and the hedge came out exactly flat. Every price above came from
`queryOrder`: both futures `newOrder` responses again returned `executedQty`
with no `avgPrice` and no `cumQuote`, in both directions. Correction 4
reproduced on demand.

### What the reconciliation showed

This is the part worth reading, because it went against the model:

```
predicted   31.77 bps   (25.00 fees + 6.77 measured round-trip spread)
realised    55.50 bps
difference  +23.73 bps
```

Realised cost exceeded not just the plan but **the scanner's own 44.32 bps
cost-to-beat, by 11.18 bps**, and 44.32 was already the twice-corrected,
deliberately conservative figure.

It decomposes exactly, which is what makes it useful rather than just bad
news. Fees behaved: 25.00 bps, as verified. The entire overshoot is the
cross-venue entry basis at the moment of the fill:

```
perp   sold 5.002382, bought back 5.004076   -0.001694 USDT    3.4 bps
spot   bought 5.017628, sold 5.004076        -0.013552 USDT   27.1 bps
                                             ----------------
price terms                                  -0.015246 USDT   30.5 bps
fees   futures 0.005003 [DERIVED] + spot 0.007513 (0.00001037 BNB @ 724.49)
                                             -0.012516 USDT   25.0 bps
                                             ----------------
net                                          -0.027762 USDT   55.5 bps
```

The spot leg bought at 0.02962 while the futures leg sold at 0.02953. That
gap is an entry basis of **30.5 bps**, paid the instant both legs were on.
The model carried 19.32 bps. `select_symbol.py`, run seconds earlier, had
measured 6.8 bps on this symbol.

**Three different values for the same quantity within one minute**, one of
them from a live measurement taken immediately before the order. That is the
whole argument, demonstrated against real money: the entry basis is not a
constant to be looked up, it is a live quantity that has to be priced at the
moment of trading, and a model reasoning in prose cannot know it. Even
measuring it and then acting a minute later was not good enough here.

The deeper problem is not that the constant was stale. It is that the
prediction used the wrong model. Round-trip spread only equals the true price
cost when the basis at the close matches the basis at the open, and there is
no reason it should. Entry basis plus exit basis reproduces this run to within
0.07 bps where the spread model was out by 23.73, which is why
`cost_oracle.py` prices it that way and assumes the basis reverts rather than
holds. Erring conservative can only make it refuse more.

The honest reading of this run is that the cost oracle was **directionally
right and still not conservative enough**. It refused every signal on the
board, and the one trade it did execute, as a plumbing test rather than on
merit, cost more than its own refusal threshold. Nothing on the board came within
30 bps of clearing, so the refusals stand regardless. But it is the reason
the future-work section proposes pricing at call time rather than caching a
constant.

## Limitations

Stated plainly, because a simplification a judge discovers themselves is
worth less than one you declare.

- **No qualifying signal existed in the observation window.** Not one of
  25,185 pair evaluations cleared 44.32 bps. The scanner printing
  `NO QUALIFYING SIGNAL` is correct behaviour, not a bug. No threshold was
  lowered to manufacture a trade.
- **The executed trade is a mechanism test, not a trade taken on merit.** It
  exists to prove the execution path is real and to measure what it actually
  costs. It is minimum size, it would not have been taken on its economics,
  and it lost 55.50 bps. Every number it produces is labelled as such.
- **The cost model was still too optimistic when it met a real fill.** 44.32
  bps was the refusal threshold; the round trip cost 55.50. The gap is entry
  basis, not fees. Since nothing on the board came within 30 bps of clearing,
  a threshold that is 11 bps too low changes no decision in this window, but it
  would matter on a board where something was close.
- **The observation window is short**, hours rather than weeks. It establishes that
  the board was flat during it, not that funding arbitrage is never viable.
  A large block of symbols prints exactly 1.000 bps, which is Binance's
  default rate and indicates a dead-calm market rather than an opportunity.
- **Futures commission in the reconciliation is derived**, not read from a
  fill: the order response does not carry it. It is computed from the
  verified 5 bps rate and explicitly labelled `[DERIVED]` in the output.
- **The constant-based path is still a snapshot.** `scanner.py` prices the
  board against `FRICTION = 0.001932`, one entry basis measured on one symbol
  at one moment, and the paired run proved how poor a snapshot is: 6.8 bps at
  plan time, 30.5 bps realised on the fill under a minute later, against the
  19.32 bps in the constant. `python select_symbol.py --refresh` re-derives it
  and the constant's comment says so, but a constant is the wrong shape for a
  quantity that moves that fast. The correction is built, not pending:
  `cost_oracle.py` and the `costcheck` MCP server measure the basis at
  evaluation time, by walking the live depth ladder at the size actually being
  traded, and expire the answer after 10 seconds. The scanner keeps its
  constant deliberately, so that all 25,185 refusals in the window are
  measured against one unchanging threshold; anything about to place an order
  goes through `evaluate_trade` instead. `executor.py` has a different defect,
  priced live but on the wrong term, in the bullet below.
- **Decisions are already partially stale by the time they arrive.** A measured
  `quote_latency_ms` of ~2,570 ms consumes over a quarter of the 10-second TTL
  simply fetching the depth books before the arithmetic even begins, meaning a
  production version would need the TTL to account for measurement latency
  rather than market movement alone.
- **The oracle is advisory. It is not a security boundary.** This is the
  most important limitation here and it would be dishonest to bury it. The
  agent still holds `spot_newOrder` and `futures_usds_newOrder` directly. It
  can call `evaluate_trade`, read `REJECT`, and place the order anyway.
  Nothing in this repository can stop it. `CLAUDE.md` requires the call, but
  that is a policy written in a file a model can ignore, not a wall. Making
  it unbypassable means the agent never holding the raw order tools at all:
  it calls a gateway, the gateway prices the trade, and only the gateway
  holds the credentials that reach Binance. That gateway is not built. What
  is built is the arithmetic it would have to run, which is the part that has
  to be correct first, and which this project has already caught itself
  getting wrong once.
- **The executor's own prediction uses the older, narrower model.**
  `executor.py` predicts the price term as round-trip spread, which is what
  produced the 31.77 bps figure the paired run missed by 23.73. The corrected
  entry-basis model lives in `cost_oracle.py`. The executor was deliberately
  left alone rather than patched between recorded runs, so the two runs in
  `trades.jsonl` are measured against the same yardstick and stay comparable.
- **Single account, single fee tier.** 7.5 bps holds while BNB burn is on and
  the BNB reserve is above dust. Either changing reverts it to 10 bps taken
  out of the base asset, which would also reintroduce a fee-driven unhedged
  residual.

## Future work

The MCP server described in the section above is built. Three things after it,
in the order they matter.

**Make the check unbypassable.** Today the agent holds the order tools and
the oracle asks nicely. The next version inverts that: the agent gets
`request_trade(...)` and nothing else, a gateway holds the Binance
credentials, and an order that arrives without a live, unexpired
authorization does not reach the venue. The `authorization` block
`evaluate_trade` already returns is shaped for exactly that handoff, which is
the cheap half of the work. The expensive half is that the gateway has to
hold credentials, and that is a different security posture from a tool that
holds none.

**Measure what the refusals were worth.** `refusals.jsonl` currently records
a decision and its arithmetic. It does not record what happened next. Logging
the funding that actually settled and the execution cost that would actually
have been paid turns each refusal from an absence into a measured saving:

```
11:00  opportunity +38 bps, cost 46 bps, REFUSED
19:00  funding actually settled at +7 bps, entry basis would have been 31 bps
       counterfactual: refusing saved 14 bps
```

Aggregated over thousands of decisions that is the number this whole project
is missing. A guardrail that refuses everything is only obviously correct once
you can show what the refusals avoided. Everything needed is already in the
log format; it needs a follow-up pass and enough elapsed time to settle.

**Give the calibration loop something to learn from.** The loop itself is
built: `calibration()` re-reads `trades.jsonl` on every call, so each
reconciled execution appended to it widens the buffer the next decision is
charged automatically, with no code change. What it lacks is executions. It
currently reads `n=1`, and that single point says the model under-charges by
16.95 bps. One point is an anecdote, and the object publishes `n` alongside
the gap precisely so a caller can see that. What turns it into a distribution,
with a direction and a per-symbol shape, is elapsed time and settled fills
rather than more software.

## Running it

```bash
pip install -r requirements.txt

python diagnose.py                  # funnel + live funding distribution
python scanner.py                   # scan the board, print the verdict
python cost_oracle.py STRKUSDT      # price one hedge against live depth
python cost_oracle.py STRKUSDT --json
python select_symbol.py             # rank hedgeable pairs by residual
python refusal_log.py --loop --every 300
python report_refusals.py           # aggregate
python build_report.py --open       # standalone report.html

python cost_mcp.py                  # the MCP server itself, stdio, for any client
```

Execution runs through the Binance MCP server and is driven by the agent
against `executor.py`; it is not a headless loop and will not trade on its
own.

## Safety

Trades execute in an isolated **Agentic sub-account** that cannot withdraw
externally. No custom kill switch was built: Binance ships one under
Sub-account → Account Management, which disconnects all agents and cancels
all positions and orders. Use theirs.
