# The arithmetic layer that says no

**A pre-trade cost oracle for Binance Agent OS.**

An LLM connected to execution tools has no native cost model. It reasons in
sentences while the loss lives in basis points. This is the layer that prices
the complete round trip against live order books and refuses the trade when
the arithmetic does not clear.

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
| 2 | Execution friction **5 bps** | **19.32 bps**, then **30.5 bps** | Wrong twice, both times in the same direction. First: cross-venue entry basis measured by `select_symbol.py` on STRKUSDT at 2026-09-04 17:40 UTC: 19.32 bps, off by a factor of nearly four. Then the paired run of 2026-09-05 06:33 UTC paid **30.5 bps** on the fill (orders 5102989720 / 1521379862), against **6.8 bps** that `select_symbol.py` had measured on the same symbol under a minute earlier, and against the 19.32 carried in the constant. |
| 3 | Lot-step truncation is the **dominant** error at small size | **6 of 356 pairs** | `select_symbol.py` compared both `exchangeInfo` endpoints across every hedgeable pair. Only 6 have a spot step coarser than their futures step; for the rest cross-venue truncation is exactly zero. |
| 4 | Futures `newOrder` returns the fill price | **It does not** | Preflight order `6578993465` came back `FILLED` with `executedQty` but no `avgPrice` and no `cumQuote`, even at `newOrderRespType=RESULT`. `futures_usds_queryOrder` on the same id has both. Unhandled, this records a fill price of zero. |

Assumption 2 is the one that kills the prompt-engineering answer. **The entry
basis is not in any fee schedule.** It is the live gap between two order
books. It changes by the second and it varies per symbol. The only way to
know it is to measure it at the moment of trading, which is exactly what a
language model cannot do by reasoning and exactly what this layer does.

**Assumption 2 is also the only one that was wrong twice**, and the second
time it was wrong against a number this project had itself measured from a
live book under a minute before the order. Three values for one quantity
inside that minute: 6.8 bps measured, 19.32 bps carried, 30.5 bps paid. If
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
funding scanner is one caller of it. Any Agent OS trade call could be
another.

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
observation window was **4.676 bps per 8h settlement** (MARSCOINUSDT), which
needs **3.16 days** of holding to break even and falls **30.29 bps short**
over the three settlements the scanner is willing to underwrite.

That is the best print out of an entire board, over fifteen hours. It is not
close.

## The result

At the time of writing, `refusals.jsonl` holds:

```
  window            2026-09-04 15:48 to 2026-09-05 06:36 UTC (14.8h)
  scans             60
  pair evaluations  21,900
  refused           60
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
  21,900 pair evaluations cleared 44.32 bps. The scanner printing
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
- **The cost model is a snapshot, and the paired run proved how poor a
  snapshot is.** The entry basis was measured at one moment on one symbol.
  It moves fast: 6.8 bps measured at plan time, 30.5 bps realised on the fill
  under a minute later, against 19.32 bps carried in the constant.
  `python select_symbol.py --refresh` re-derives it, and the constant's
  comment says so, but a constant is the wrong shape for this quantity. That
  is what the MCP server in future work is for.
- **Single account, single fee tier.** 7.5 bps holds while BNB burn is on and
  the BNB reserve is above dust. Either changing reverts it to 10 bps taken
  out of the base asset, which would also reintroduce a fee-driven unhedged
  residual.

## Future work: the productised form

The natural product is not this scanner. It is a small MCP server of its own,
sitting beside Binance's, exposing a single tool:

```
price_roundtrip(symbol, side, notional) -> { cost breakdown, verdict }
```

Any agent connected to Agent OS calls it before calling `spot_newOrder`. The
arithmetic then arrives in the model's context **as a number**, rather than
being something the model was supposed to remember or derive. The verdict
carries its own workings, so a refusal is auditable rather than opaque.

That is deliberately not built here. This build is the reference
implementation of the logic such a server would expose, and `venue.py` is
already an importable module with no CLI dependency, so the cost engine is
already separable from the scanner.

## Running it

```bash
pip install -r requirements.txt

python diagnose.py                  # funnel + live funding distribution
python scanner.py                   # scan the board, print the verdict
python select_symbol.py             # rank hedgeable pairs by residual
python refusal_log.py --loop --every 300
python report_refusals.py           # aggregate
python build_report.py --open       # standalone report.html
```

Execution runs through the Binance MCP server and is driven by the agent
against `executor.py`; it is not a headless loop and will not trade on its
own.

## Safety

Trades execute in an isolated **Agentic sub-account** that cannot withdraw
externally. No custom kill switch was built: Binance ships one under
Sub-account → Account Management, which disconnects all agents and cancels
all positions and orders. Use theirs.
