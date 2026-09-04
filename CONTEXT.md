# CONTEXT.md

Read this fully before writing code. You have no prior context on this project.

---

## 1. What this is

A submission to the **Binance Agent OS Mini Hackathon**, Track A (build an AI
agent with Agent OS, 20,000 USDC). Deadline: **Sept 8 2026, 23:59 UTC**.
Deliverable is a video/demo plus a public GitHub repo.

Time remaining is roughly two days including video production. Scope
accordingly. Nothing speculative gets built.

## 2. The thesis (this is the product, not the bot)

An LLM connected to trading tools will execute unprofitable trades with
complete confidence, because it reasons in natural language while the loss
lives in basis points. Ask any agent to "farm the highest funding rate on
Binance" and it will do so correctly and lose money, because it does not
compute the round-trip cost of both legs of the hedge.

**This project is the arithmetic layer that says no.**

The agent scans the entire USD-M perpetual board for delta-neutral funding
capture opportunities (long spot / short perp), computes the full cost stack,
and refuses every trade that does not clear it. The refusal is the feature.

## 3. Established facts (verified, do not re-derive)

These were confirmed against the live account and live API. Treat as ground truth.

| Fact | Value |
|---|---|
| Spot taker fee (this account) | 0.00100000 (10 bps) |
| Futures USD-M taker fee | ~5 bps |
| Round-trip cost, both legs | ~30 bps |
| Assumed slippage + residual friction | 5 bps |
| Total cost to beat | **35 bps** |
| Perps on futures | 898 |
| ...with a matching USDT spot pair | 365 |
| ...with positive funding | 308 |
| Best funding rate on the board | ~4.17 bps / 8h (COOKIEUSDT) |
| Break-even hold on the best symbol | ~2.8 days |

**Consequence: no qualifying signal exists in the available window.** The
scanner printing `NO QUALIFYING SIGNAL` is correct behaviour, not a bug.
Do not lower thresholds to manufacture a trade.

A large block of symbols prints funding of exactly 1.000 bps. That is
Binance's default rate and indicates a flat market, not an opportunity.

## 4. Connection and permissions

- Binance MCP server: `https://agent.binance.com/mcp/agentic`, Streamable HTTP.
- Already authenticated via OAuth. **Do not run `claude mcp remove`.** The
  connection is working; a removal would break it.
- Confirm-before-execute is Claude Code's own permission prompt, not Binance
  server enforcement. `spot_newOrder`, `futures_usds_newOrder` and
  `wallet_userUniversalTransfer` take no confirmation token. Verified by a
  single-round-trip BNB market sell that filled immediately.
- Trades execute in an isolated **Agentic sub-account**. It cannot withdraw
  externally or move funds to the main account.
- Emergency Stop exists in the Binance UI under Sub-account > Account
  Management. It disconnects all agents and cancels all positions and orders.
  **Do not build a kill switch. Demo theirs.**

## 5. Capital constraints (these are severe, respect them)

Total available: **~$15 USD**, split across the spot and USD-M wallets.

- Minimum notional is ~$5 per leg. Two legs at minimum is ~$10.
- Lot-step truncation is the dominant error at this size. A 5 USDT spot order
  already truncated to 0.006 BNB (~$4.30) in testing.
- Spot and futures have **different** `stepSize` and `minNotional` for the same
  asset. A symmetric hedge is never actually symmetric.
- Run the futures leg at **1x**. No leverage.

**Symbol selection for the mechanism test is by lot-step granularity, not by
funding rate.** Choose a low-priced asset where one lot step is worth a few
cents, so the residual unhedged delta is a small fraction of notional. Query
both `exchangeInfo` endpoints and rank candidates by the size of the residual
you would be left holding.

## 6. Architecture

Market data uses **public REST only**. No auth, no MCP. Round-tripping numeric
data through an LLM is slow and pointless.

- `https://fapi.binance.com/fapi/v1/premiumIndex` (funding, mark price, all perps)
- `https://fapi.binance.com/fapi/v1/fundingRate` (historical, for persistence)
- `https://api.binance.com/api/v3/exchangeInfo` (spot universe, lot filters)

MCP is used **only** for execution and account state.

Existing files: `scanner.py` (signal engine, working), `diagnose.py`
(distribution and funnel visibility, working). Build around them, do not
rewrite them.

## 7. Execution rules (non-negotiable)

1. **Fill-driven sizing.** Open the futures leg first. Read the *actual filled
   quantity* from the response. Truncate the spot leg to spot's own `stepSize`
   against that number. Never size both legs from the same intended figure.
2. **Log the residual.** There will always be an unhedged remainder from step
   truncation. Compute it, log it in both units and USD, surface it. Knowing
   its size is the point; pretending it is zero is the failure.
3. **Partial fill fallback.** If leg two fails or fills short, immediately
   reduce leg one to match rather than leaving directional exposure open.
4. **No fabricated numbers anywhere.** Every figure in output, logs, README and
   video must come from a live call. If a value is assumed, label it assumed.

## 8. Build order

**Now:** symbol selection utility. Pull spot and futures `exchangeInfo`, compute
for each candidate the residual delta a ~$5-per-leg hedge would leave. Output a
ranked shortlist. This decides what the mechanism test trades.

**Then:** the executor. Fill-driven sizing per section 7, both legs, full
reconciliation log with order IDs, then close both legs and log realised cost.

**Then:** run `scanner.py` on a loop and persist every scan to disk. The log of
refusals is the primary demo artifact. Start it early; it needs hours of history.

**Then:** README. Architecture, the cost model with real numbers, the 898-refusal
result, and an explicit limitations section.

**Last:** video.

## 9. Explicitly out of scope

Rolling z-scores. Any database. Basis-convergence trading. Multi-strategy
support. A custom kill switch. Backtesting. Anything that makes the scanner
fire by weakening its arithmetic.

## 10. Demo mode

If a lower threshold is used to demonstrate the execution path, it must be a
separate, clearly named mode, labelled in the code and stated aloud in the
video: *"the signal did not clear, so this is a mechanism test at minimum size,
not a trade I would take."*

Judges do not penalise a stated simplification. They penalise one they discover
themselves.
