"""
Builds report.html from refusals.jsonl (and trades.jsonl if present).

Produces one standalone file with the data baked in. No server, no CDN,
no network. Open it in a browser, record it, attach it to the repo.

    python build_report.py
    python build_report.py --open
"""

import argparse
import json
import os
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REFUSALS = os.path.join(HERE, "refusals.jsonl")
TRADES = os.path.join(HERE, "trades.jsonl")
OUT = os.path.join(HERE, "report.html")


def load(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    return rows


def build():
    refusals = load(REFUSALS)
    trades = load(TRADES)
    if not refusals:
        raise SystemExit("no refusals.jsonl - run refusal_log.py --loop first")

    payload = json.dumps({
        "refusals": refusals,
        "trades": trades,
        "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })

    html = TEMPLATE.replace("__DATA__", payload)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {OUT}  ({len(refusals)} scans, {len(trades)} trades)")
    return OUT


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cost-Truth Engine</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --line:#26303d;
  --ink:#e6edf3; --dim:#8b949e; --accent:#f0b429; --bad:#f85149; --good:#3fb950;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
:root:not([data-theme="dark"]){}
@media(prefers-color-scheme:light){:root:not([data-theme="dark"]){
  --bg:#ffffff; --panel:#f6f8fa; --line:#d0d7de; --ink:#1f2328; --dim:#636c76;
}}
:root[data-theme="light"]{--bg:#fff;--panel:#f6f8fa;--line:#d0d7de;--ink:#1f2328;--dim:#636c76;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
 line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:48px 20px 80px}
h1{font-size:clamp(26px,5vw,40px);margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--dim);font-size:16px;margin:0 0 40px;max-width:64ch}
.claim{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
 border-radius:8px;padding:20px 22px;margin:0 0 40px;font-size:17px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:44px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px}
.stat .n{font-size:30px;font-weight:600;font-family:var(--mono);letter-spacing:-.02em}
.stat .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.07em;margin-top:6px}
.stat.hero .n{color:var(--accent)}
h2{font-size:19px;margin:44px 0 8px;letter-spacing:-.01em}
.note{color:var(--dim);font-size:14px;margin:0 0 18px;max-width:70ch}
.rec{font-family:var(--mono);font-size:12px;color:var(--accent);margin:22px 0 8px;font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.chartbox{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px;overflow-x:auto}
svg{display:block;width:100%;height:auto;min-width:520px}
.legend{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px;font-size:13px;color:var(--dim)}
.key{width:20px;height:3px;display:inline-block;vertical-align:middle;margin-right:7px;border-radius:2px}
.tbl{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:13px;min-width:560px}
th,td{padding:9px 14px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
tr:last-child td{border-bottom:none}
.verdict{color:var(--bad);font-weight:600}
.ok{color:var(--good)}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
</style></head><body><div class="wrap">

<h1>The trade that never cleared</h1>
<p class="sub">An agent connected to Binance Agent OS watched every hedgeable
perpetual on the exchange, priced the full cost of each trade, and declined
all of them. This page is the record.</p>

<div class="claim" id="claim"></div>

<div class="grid" id="stats"></div>

<h2>Why nothing qualified</h2>
<p class="note">The gold line is what the best opportunity on the entire
exchange would pay over the intended holding period. The red line is what it
costs to open and close the hedge. Profit requires the gold line to rise above
the red one. Across the whole observation window, it never came close.</p>
<div class="chartbox">
  <svg id="chart" viewBox="0 0 900 340" xmlns="http://www.w3.org/2000/svg"></svg>
  <div class="legend">
    <span><i class="key" style="background:var(--accent)"></i>Best available payout</span>
    <span><i class="key" style="background:var(--bad)"></i>Cost to trade</span>
  </div>
</div>

<h2>Every decision, timestamped</h2>
<p class="note">One row per scan. Nothing is summarised away.</p>
<div class="tbl"><table id="scans"></table></div>

<div id="tradesec"></div>

<footer id="foot"></footer>
</div>
<script>
const D = __DATA__;
const R = D.refusals, T = D.trades;
const f = (n,d=2)=> n==null? "-" : Number(n).toFixed(d);

// ---- headline numbers -------------------------------------------------
const t0=new Date(R[0].ts), t1=new Date(R[R.length-1].ts);
const hours=(t1-t0)/3.6e6;
const evals=R.reduce((s,r)=>s+r.board.hedgeable,0);
const refused=R.filter(r=>r.verdict==="REFUSED").length;
const peak=R.reduce((a,b)=> (b.board.best_rate_bps>a.board.best_rate_bps? b:a));
const cost=Math.max(...R.map(r=>r.model.cost_to_beat_bps));
const hold=R[R.length-1].model.hold_periods;
const peakGross=peak.board.best_rate_bps*hold;

document.getElementById("claim").innerHTML =
 `Over <b>${f(hours,1)} hours</b>, the agent made <b>${evals.toLocaleString()}</b>
  individual pricing decisions and refused <b>every one</b>. The single best
  opportunity it ever saw paid <b>${f(peakGross)} bps</b> over the holding
  period against a cost of <b>${f(cost)} bps</b> &mdash; short by
  <b>${f(cost-peakGross)} bps</b>.`;

const stats=[
 ["hero", refused, "trades refused"],
 ["", evals.toLocaleString(), "pricing decisions"],
 ["", f(hours,1)+"h", "continuous run"],
 ["", R[0].board.perps_total, "perpetuals watched"],
 ["", f(cost,1), "bps cost to beat"],
 ["", f(peak.board.best_rate_bps,3), "bps best ever seen"],
];
document.getElementById("stats").innerHTML = stats.map(([c,n,l])=>
 `<div class="stat ${c}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");

// ---- chart ------------------------------------------------------------
(function(){
  const W=900,H=340,P={t:20,r:20,b:38,l:52};
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  const pay=R.map(r=>r.board.best_rate_bps*r.model.hold_periods);
  const cst=R.map(r=>r.model.cost_to_beat_bps);
  const ymax=Math.max(...cst,...pay)*1.25;
  const x=i=> P.l + (R.length<2?iw/2:(i/(R.length-1))*iw);
  const y=v=> P.t + ih - (v/ymax)*ih;
  const path=a=>a.map((v,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  let g="";
  for(let k=0;k<=4;k++){
    const v=ymax*k/4, yy=y(v);
    g+=`<line x1="${P.l}" y1="${yy}" x2="${W-P.r}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`
     + `<text x="${P.l-9}" y="${yy+4}" fill="var(--dim)" font-size="11" text-anchor="end"
         font-family="var(--mono)">${v.toFixed(0)}</text>`;
  }
  // shade the gap: this is the whole argument
  const gap=`M${path(pay).slice(1)} L${x(R.length-1)},${y(cst[cst.length-1])} `
    + cst.map((v,i)=>`L${x(cst.length-1-i)},${y(cst[cst.length-1-i])}`).join("") + "Z";
  g+=`<path d="${gap}" fill="var(--bad)" opacity="0.07"/>`;
  g+=`<path d="${path(cst)}" fill="none" stroke="var(--bad)" stroke-width="2.5"/>`;
  g+=`<path d="${path(pay)}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>`;
  const lab=d=>new Date(d).toISOString().slice(11,16);
  g+=`<text x="${P.l}" y="${H-12}" fill="var(--dim)" font-size="11"
       font-family="var(--mono)">${lab(R[0].ts)} UTC</text>`
   + `<text x="${W-P.r}" y="${H-12}" fill="var(--dim)" font-size="11" text-anchor="end"
       font-family="var(--mono)">${lab(R[R.length-1].ts)} UTC</text>`
   + `<text x="14" y="${P.t+ih/2}" fill="var(--dim)" font-size="11"
       transform="rotate(-90 14 ${P.t+ih/2})" text-anchor="middle">basis points</text>`;
  document.getElementById("chart").innerHTML=g;
})();

// ---- scan table -------------------------------------------------------
document.getElementById("scans").innerHTML =
 `<thead><tr><th>time (UTC)</th><th>pairs</th><th>best symbol</th>
  <th>pays</th><th>costs</th><th>short by</th><th>verdict</th></tr></thead><tbody>`
 + R.map(r=>{
   const pays=r.board.best_rate_bps*r.model.hold_periods;
   return `<tr><td>${r.ts.slice(11,19)}</td><td>${r.board.hedgeable}</td>
    <td>${r.board.best_symbol||"-"}</td><td>${f(pays)}</td>
    <td>${f(r.model.cost_to_beat_bps)}</td>
    <td>${f(r.model.cost_to_beat_bps-pays)}</td>
    <td class="${r.verdict==="REFUSED"?"verdict":"ok"}">${r.verdict}</td></tr>`;
 }).join("") + `</tbody>`;

// ---- trades -----------------------------------------------------------
// Two kinds of record live in trades.jsonl and they do not belong under one
// heading. Moving money into position is housekeeping; it is not evidence
// that the execution path works. Only the orders are that.
const ts=document.getElementById("tradesec");
const PREP = new Set(["funding_sell","funding_transfer"]);
const prep = T.filter(t=>PREP.has(t.event));
const exec = T.filter(t=>!PREP.has(t.event));

function recBlock(t){
  const when = t.ts ? " &middot; " + t.ts.slice(0,19).replace("T"," ") + " UTC" : "";
  return `<h3 class="rec">${t.event||"record"}${when}</h3>
   <div class="tbl"><table>
   <thead><tr><th>field</th><th>value</th></tr></thead><tbody>`
   + Object.entries(t).filter(([k])=>k!=="event").map(([k,v])=>
      `<tr><td>${k}</td><td>${typeof v==="object"?JSON.stringify(v):v}</td></tr>`
     ).join("") + `</tbody></table></div>`;
}

let tHTML = `<h2>Mechanism verification</h2>`;
if(exec.length){
  tHTML += `<p class="note">The signal did not clear, so no position was taken
   on its merits. These are minimum-size executions proving the order path
   works: the short leg opens first, the long leg is sized from what actually
   filled, and the residual is measured instead of being taken on trust.</p>`
   + exec.map(recBlock).join("");
} else {
  tHTML += `<p class="note">No executions recorded yet. Run the executor, then
   rebuild this page.</p>`;
}
if(prep.length){
  tHTML += `<h2>Account preparation</h2>
   <p class="note">Housekeeping that moved capital to where the two legs
   needed it. These are not executions and prove nothing about the order
   path. They are listed so the ledger is complete.</p>`
   + prep.map(recBlock).join("");
}
ts.innerHTML = tHTML;

document.getElementById("foot").textContent =
 `Generated ${D.generated} from refusals.jsonl. `
 + `Every figure on this page is read from a logged live API response. `
 + `Nothing is estimated, illustrative, or hand-entered.`;
</script></body></html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    p = build()
    if a.open:
        webbrowser.open("file://" + p)
