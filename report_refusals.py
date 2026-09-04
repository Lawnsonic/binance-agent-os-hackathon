"""
Reads refusals.jsonl and prints the aggregate.

This output is the demo artifact. Run it on camera.
"""

import json
import os
import sys
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "refusals.jsonl")


def load(path=LOG_PATH):
    if not os.path.exists(path):
        sys.exit(f"no log at {path} - run refusal_log.py --loop first")
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue          # tolerate a torn final line
    return rows


def main():
    rows = load()
    if not rows:
        sys.exit("log is empty")

    first = datetime.fromisoformat(rows[0]["ts"])
    last = datetime.fromisoformat(rows[-1]["ts"])
    hours = (last - first).total_seconds() / 3600

    refused = sum(1 for r in rows if r["verdict"] == "REFUSED")
    traded = len(rows) - refused
    evaluated = sum(r["board"]["hedgeable"] for r in rows)

    bests = [r["board"]["best_rate_bps"] for r in rows
             if r["board"]["best_rate_bps"] is not None]
    peak = max(bests) if bests else 0.0
    peak_row = max(rows, key=lambda r: r["board"]["best_rate_bps"] or 0)

    costs = {r["model"]["cost_to_beat_bps"] for r in rows}

    print()
    print(f"  window            {first:%Y-%m-%d %H:%M} to {last:%H:%M} UTC "
          f"({hours:.1f}h)")
    print(f"  scans             {len(rows)}")
    print(f"  pair evaluations  {evaluated:,}")
    print(f"  refused           {refused}")
    print(f"  traded            {traded}")
    print()
    print(f"  cost to beat      {'/'.join(f'{c:.0f}' for c in sorted(costs))} bps")
    print(f"  best ever seen    {peak:.3f} bps/8h  "
          f"({peak_row['board']['best_symbol']} at {peak_row['ts']})")
    if peak > 0:
        need = max(costs)
        print(f"  ...which needed   {need / peak / 3:.1f} days to break even")
        print(f"  ...short by       {need - peak * 3:.1f} bps over 3 periods")
    print()

    if refused == len(rows):
        print("  Every opportunity on the board was refused. "
              "Not one cleared cost.")
    print()


if __name__ == "__main__":
    main()
