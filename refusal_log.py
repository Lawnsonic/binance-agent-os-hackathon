"""
Refusal log.

Runs the scanner on a timer and appends one JSON line per evaluation to
refusals.jsonl. Does not modify scanner.py; it imports it, so the cost model
being logged is always the one the scanner actually used.

The point is the record, not the run. A single screenshot of NO QUALIFYING
SIGNAL is a claim. Thousands of timestamped evaluations, each carrying the
best opportunity available on the whole board at that moment and how far
short of cost it fell, is evidence.

    python refusal_log.py                 # one evaluation, appended
    python refusal_log.py --loop          # every 5 minutes until stopped
    python refusal_log.py --loop --every 180
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import scanner
import venue

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "refusals.jsonl")

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True
    print("\nstop requested, finishing current cycle...", flush=True)


def board_state():
    """
    What was actually available at this moment, independent of whether
    anything qualified. This is the number that makes the log persuasive:
    not 'nothing fired' but 'the best thing on the entire board was X,
    and X needed N days to break even'.
    """
    spot = scanner.spot_universe()
    snap = venue.http_get(f"{venue.FAPI}/fapi/v1/premiumIndex")

    hedgeable = [r for r in snap if r["symbol"] in spot]
    positive = [(r["symbol"], float(r.get("lastFundingRate") or 0))
                for r in hedgeable
                if float(r.get("lastFundingRate") or 0) > 0]

    best_sym, best_rate = (None, 0.0)
    if positive:
        best_sym, best_rate = max(positive, key=lambda p: p[1])

    return {
        "perps_total": len(snap),
        "hedgeable": len(hedgeable),
        "funding_positive": len(positive),
        "best_symbol": best_sym,
        "best_rate_bps": round(best_rate * 10_000, 4),
    }


def evaluate():
    cost = scanner.ROUND_TRIP + scanner.FRICTION
    state = board_state()

    best = state["best_rate_bps"] / 10_000
    if best > 0:
        periods = cost / best
        state["breakeven_periods"] = round(periods, 2)
        state["breakeven_days"] = round(periods / 3, 2)
        state["shortfall_bps"] = round((cost - best * scanner.HOLD_PERIODS)
                                       * 10_000, 2)
    else:
        state["breakeven_periods"] = None
        state["breakeven_days"] = None
        state["shortfall_bps"] = None

    qualifying = scanner.scan()

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # The model is logged with every line, so a mid-run constant change
        # is visible in the record instead of silently rewriting history.
        "model": {
            "round_trip_bps": round(scanner.ROUND_TRIP * 10_000, 2),
            "friction_bps": round(scanner.FRICTION * 10_000, 2),
            "cost_to_beat_bps": round(cost * 10_000, 2),
            "hold_periods": scanner.HOLD_PERIODS,
            "min_edge_bps": round(scanner.MIN_EDGE * 10_000, 2),
        },
        "board": state,
        "qualifying_count": len(qualifying),
        "qualifying": [c["symbol"] for c in qualifying],
        "verdict": "TRADE" if qualifying else "REFUSED",
    }


def append(entry):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def line(entry):
    b = entry["board"]
    return (f"{entry['ts']}  {entry['verdict']:<8}"
            f"eval={b['hedgeable']:>4}  "
            f"best={b['best_symbol'] or '-':<12}"
            f"{b['best_rate_bps']:>7.3f}bps  "
            f"need={entry['model']['cost_to_beat_bps']:.0f}bps  "
            f"breakeven={b['breakeven_days'] or float('nan'):.1f}d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every", type=int, default=300,
                    help="seconds between evaluations (default 300)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    n = 0
    while True:
        try:
            entry = evaluate()
            append(entry)
            n += 1
            print(line(entry), flush=True)
        except Exception as e:
            # A failed cycle must not kill the run. The log is only worth
            # anything if it is continuous.
            print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                  f"  ERROR  {type(e).__name__}: {e}", file=sys.stderr,
                  flush=True)

        if not args.loop or _stop:
            break

        for _ in range(args.every):
            if _stop:
                break
            time.sleep(1)
        if _stop:
            break

    print(f"\n{n} evaluations appended to {LOG_PATH}")


if __name__ == "__main__":
    main()
