# Trading policy

This repository connects to Binance Agent OS, where a tool call moves real
money in a real account. The rules below are not style preferences.

## Before any order

**Call `evaluate_trade` from the `costcheck` MCP server before calling
`spot_newOrder`, `futures_usds_newOrder`, or any other tool that opens or
closes a position.**

- `decision: "REJECT"` is binding. Do not place the order. Report the
  `reason` and the `shortfall_bps` and stop.
- `decision: "APPROVE"` authorises the size in `max_notional_usdt` on that
  symbol, and nothing else. It is not general permission to trade.
- A decision expires at `expires_at`, which is seconds after it was measured.
  That is deliberate: on 2026-09-05 the cross-venue basis on STRKUSDT moved
  from 13.55 to 30.50 bps inside one minute. If the decision has expired, call
  again. Do not act on a stale one.
- If the tool returns `reason: "pricing_failed"`, the cost is unknown rather
  than acceptable. Unknown cost means no order.

Do not compute the cost yourself and do not estimate it in prose. That is the
specific failure this project exists to document: every cost assumption made
in natural language during this build was wrong when it was finally measured,
four out of four.

## What this policy is, honestly

A policy, not a wall. You still hold the raw order tools and nothing here can
physically stop you from ignoring this file. Making the check unbypassable
requires a gateway that holds the Binance credentials and refuses to forward
an unpriced order, which is not built. Until it is, this rule works only if
it is followed.

## Standing constraints

- **Never lower a threshold to make the scanner fire.** If nothing clears,
  `NO QUALIFYING SIGNAL` is the correct output. A refusal is the product.
- **No fabricated numbers.** Every figure in code, logs, the README or the
  video must come from a live call. If a value is assumed, label it assumed.
- **Fill-driven sizing.** Futures leg first. Size the spot leg from leg one's
  actual `executedQty`, truncated to the spot `stepSize`. Never size both
  legs from the same intended figure.
- **Futures `newOrder` does not return a fill price.** No `avgPrice`, no
  `cumQuote`, even at `newOrderRespType=RESULT`. Always call
  `futures_usds_queryOrder` for the real fill. Skipping this records a fill
  price of zero.
- **Unwind rather than hold exposure.** If leg two fails or fills short,
  reduce leg one to match immediately and log the unwind as its own record.
- **Do not run `claude mcp remove`.** The Binance OAuth connection is working.
- **Do not build a kill switch.** Binance ships one under Sub-account >
  Account Management. Use theirs.
- Market data is public REST only. MCP is for execution and account state.
