#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2","pyyaml>=6"]
# ///
"""The factory observatory — a served window into every run.

Reads the SQLite trace mirror (obsdb) and serves a JSON API + a single-page dashboard.
Network-addressable on purpose (an HTTP server, not a file:// page) so the same view works
whether the run is local or, later, streaming from a sandbox. Polls for live updates.

    uv run observe.py [--host 127.0.0.1] [--port 7788] [--ingest]
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CP = Path(__file__).resolve().parent / "control-plane"
sys.path.insert(0, str(CP))
import obsdb


# ----------------------------------------------------------------------------- shaping
def structure_run(run: dict) -> dict:
    """Group the flat event stream into an ordered timeline of phases, gates and markers."""
    tl = []
    cur = None
    for e in run["events"]:
        ev = e["event"]
        detail = json.loads(e["detail"]) if e.get("detail") else {}
        if ev == "phase_start":
            cur = {"type": "phase", "name": e["phase"], "kind": e["kind"], "owner": e["owner"],
                   "ts": e["ts"], "items": [], "status": "running"}
            tl.append(cur)
        elif ev == "phase_end":
            if cur and cur["name"] == e["phase"]:
                cur["status"] = e["status"]; cur["ts_end"] = e["ts"]; cur = None
        elif ev == "gate":
            g = {"kind": "gate", "gate": e["gate"], "passed": bool(e["passed"]),
                 "evidence": detail.get("evidence", "")}
            (cur["items"].append(g) if cur else tl.append({"type": "gate", "ts": e["ts"], **g}))
        elif ev == "agent_call":
            c = {"kind": "call", "role": e["role"], "events": detail.get("events"),
                 "cost": detail.get("cost_usd"), "timed_out": detail.get("timed_out")}
            (cur["items"].append(c) if cur else tl.append({"type": "call", "ts": e["ts"], **c}))
        elif ev == "log":
            d = detail.get("event_detail")
            if d == "fix_iter":
                tl.append({"type": "iter", "i": detail.get("i"), "ts": e["ts"]})
            elif d in ("permission_breach", "canon_gap", "review_verdict", "converged",
                       "not_converged", "verdict", "finish"):
                tl.append({"type": "marker", "detail": d, "data": detail, "ts": e["ts"]})
    run["timeline"] = tl
    run.pop("events", None)
    return run


# ----------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/" or path == "/index.html":
                return self._send(200, SPA, "text/html; charset=utf-8")
            if path == "/healthz":
                return self._json({"ok": True})
            if path == "/api/runs":
                conn = obsdb.connect()
                try:
                    return self._json({"runs": obsdb.list_runs(conn)})
                finally:
                    conn.close()
            if path.startswith("/api/runs/"):
                rid = path[len("/api/runs/"):]
                conn = obsdb.connect()
                try:
                    run = obsdb.get_run(conn, rid)
                    return self._json(structure_run(run)) if run else self._json({"error": "not found"}, 404)
                finally:
                    conn.close()
            return self._send(404, "not found", "text/plain")
        except Exception as ex:
            return self._json({"error": str(ex)}, 500)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--ingest", action="store_true", help="backfill the DB from run files, then serve")
    a = ap.parse_args(argv)
    if a.ingest:
        conn = obsdb.connect()
        n = obsdb.ingest_all(conn)
        conn.close()
        print(f"ingested {n} runs into {obsdb.DB_PATH}")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"factory observatory → http://{a.host}:{a.port}   (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ----------------------------------------------------------------------------- the dashboard
SPA = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Factory · Observatory</title>
<style>
:root{--ink:#12161c;--surf:#1b212a;--surf2:#20272f;--line:#2b333f;--fg:#e8ecf2;--dim:#9aa6b4;--mute:#6b7686;
--accent:#e7a94b;--accent-ink:#1a140a;--pass:#5bb07f;--fail:#e0655c;--wait:#e7a94b;--info:#6ea8d8;
--bg:var(--ink);--txt:var(--fg);--brd:var(--line);
--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media(prefers-color-scheme:light){:root{--bg:#eef1f4;--surf:#fbfcfd;--surf2:#f1f4f7;--brd:#d3dae2;--txt:#171c22;--dim:#4b5563;--mute:#7a8697}}
:root[data-theme="light"]{--bg:#eef1f4;--surf:#fbfcfd;--surf2:#f1f4f7;--brd:#d3dae2;--txt:#171c22;--dim:#4b5563;--mute:#7a8697}
:root[data-theme="dark"]{--bg:var(--ink);--surf:#1b212a;--surf2:#20272f;--brd:var(--line);--txt:var(--fg);--dim:var(--dim);--mute:var(--mute)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono)}
header{display:flex;align-items:center;gap:14px;padding:11px 18px;border-bottom:1px solid var(--brd);background:var(--surf);position:sticky;top:0;z-index:5}
header .brand{font-family:var(--mono);font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
header .sub{color:var(--mute);font-size:12px;font-family:var(--mono)}
header .live{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--mute);display:flex;align-items:center;gap:7px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--pass);box-shadow:0 0 0 0 color-mix(in srgb,var(--pass) 60%,transparent);animation:p 2s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--pass) 55%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
@media(prefers-reduced-motion:reduce){.pulse{animation:none}}
.layout{display:grid;grid-template-columns:340px 1fr;height:calc(100vh - 49px)}
@media(max-width:760px){.layout{grid-template-columns:1fr;height:auto}}
.list{border-right:1px solid var(--brd);overflow-y:auto;background:var(--bg)}
.run{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--brd);background:transparent;color:inherit;
padding:12px 14px 12px 16px;cursor:pointer;position:relative}
.run:hover{background:var(--surf2)}
.run.sel{background:var(--surf)}
.run.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.run .top{display:flex;align-items:center;gap:8px;justify-content:space-between}
.run .tgt{font-weight:600;letter-spacing:-.01em}
.run .meta{font-family:var(--mono);font-size:11px;color:var(--mute);margin-top:3px;display:flex;gap:10px;flex-wrap:wrap}
.pill{font-family:var(--mono);font-size:10px;letter-spacing:.03em;padding:2px 7px;border-radius:2px;white-space:nowrap}
.p-acc{background:color-mix(in srgb,var(--pass) 24%,transparent);color:var(--txt)}
.p-rej{background:color-mix(in srgb,var(--fail) 22%,transparent);color:var(--txt)}
.p-run{background:color-mix(in srgb,var(--wait) 26%,transparent);color:var(--txt)}
.p-non{background:color-mix(in srgb,var(--mute) 24%,transparent);color:var(--txt)}
.detail{overflow-y:auto;padding:20px clamp(16px,3vw,30px)}
.dhead{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:baseline;border-bottom:1px solid var(--brd);padding-bottom:16px;margin-bottom:8px}
.dhead h1{font-size:22px;margin:0;letter-spacing:-.02em}
.dhead .lane{font-family:var(--mono);font-size:12px;color:var(--accent);text-transform:uppercase;letter-spacing:.1em}
.kv{display:flex;gap:20px;flex-wrap:wrap;font-family:var(--mono);font-size:12px;color:var(--dim);margin-left:auto}
.kv b{color:var(--txt);font-weight:600}
.reason{font-size:13px;color:var(--dim);margin:12px 0 4px;padding:9px 12px;border-left:2px solid var(--accent);background:var(--surf)}
.tl{margin-top:16px;display:flex;flex-direction:column;gap:9px;padding-bottom:40px}
.phase{border:1px solid var(--brd);border-radius:5px;background:var(--surf);overflow:hidden}
.phase .ph{display:flex;align-items:center;gap:10px;padding:10px 13px}
.phase .badge{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;padding:2px 7px;border-radius:2px}
.b-agent{background:color-mix(in srgb,var(--accent) 20%,transparent);color:var(--txt)}
.b-code{background:color-mix(in srgb,var(--info) 22%,transparent);color:var(--txt)}
.phase .nm{font-weight:600}
.phase .own{font-family:var(--mono);font-size:11px;color:var(--mute)}
.sdot{width:8px;height:8px;border-radius:50%;margin-left:auto;flex:none}
.d-ok{background:var(--pass)}.d-fail{background:var(--fail)}.d-run{background:var(--wait)}
.items{border-top:1px solid var(--brd);padding:8px 13px;display:flex;flex-direction:column;gap:6px;background:var(--bg)}
.chip{font-family:var(--mono);font-size:11.5px;display:inline-flex;align-items:center;gap:7px}
.chip .g{width:15px;height:15px;border-radius:3px;display:grid;place-items:center;font-size:10px;flex:none;color:#fff}
.g.ok{background:var(--pass)}.g.no{background:var(--fail)}
.call{font-family:var(--mono);font-size:11.5px;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap}
.call b{color:var(--txt)}
.to{color:var(--wait)}
.gate-solo{font-family:var(--mono);font-size:11.5px;padding:6px 13px;border:1px dashed var(--brd);border-radius:4px;display:flex;gap:8px;align-items:center;color:var(--dim)}
.iter{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);
display:flex;align-items:center;gap:10px;margin:6px 0 2px}
.iter::before,.iter::after{content:"";height:1px;background:var(--brd);flex:1}
.marker{font-family:var(--mono);font-size:12px;padding:9px 13px;border-radius:4px;border:1px solid var(--brd);background:var(--surf2)}
.marker.final{border-color:var(--accent)}
.marker .lbl{color:var(--accent);text-transform:uppercase;letter-spacing:.08em;font-size:10.5px}
.empty{color:var(--mute);padding:40px;text-align:center;font-family:var(--mono);font-size:13px}
.ev{font-family:var(--mono);font-size:11px;color:var(--mute);white-space:pre-wrap;word-break:break-word;margin-top:2px}
</style></head><body>
<header>
  <span class="brand">Factory · Observatory</span>
  <span class="sub" id="count">—</span>
  <span class="live"><span class="pulse"></span> live · polling 2s</span>
</header>
<div class="layout">
  <div class="list" id="list"></div>
  <div class="detail" id="detail"><div class="empty">Select a run to inspect its pipeline.</div></div>
</div>
<script>
const $=(s,r=document)=>r.querySelector(s), el=(t,c,h)=>{const n=document.createElement(t);if(c)n.className=c;if(h!=null)n.innerHTML=h;return n};
let sel=null, runs=[];
const ago=t=>{if(!t)return"";const s=Date.now()/1000-t;if(s<60)return Math.round(s)+"s ago";if(s<3600)return Math.round(s/60)+"m ago";if(s<86400)return Math.round(s/3600)+"h ago";return Math.round(s/86400)+"d ago"};
const money=c=>"$"+(c||0).toFixed(2);
function runStatus(r){if(r.accepted===1)return["p-acc","accepted"];if(r.accepted===0)return["p-rej","rejected"];if(r.finished_at)return["p-non","done"];return["p-run","running"]}
function renderList(){
  $("#count").textContent=runs.length+" runs";
  const box=$("#list");box.innerHTML="";
  runs.forEach(r=>{
    const [cls,lbl]=runStatus(r);
    const b=el("button","run"+(sel===r.run_id?" sel":""));
    b.onclick=()=>{sel=r.run_id;renderList();loadDetail(r.run_id)};
    const top=el("div","top");
    top.append(el("span","tgt",r.target||r.run_id), el("span","pill "+cls,lbl));
    const meta=el("div","meta");
    meta.append(el("span",null,(r.lane||"—")), el("span",null,r.phases+" phases"),
                el("span",null,money(r.cost_usd)), el("span",null,ago(r.started_at)));
    if(r.gates_failed)meta.append(el("span",null,"✗"+r.gates_failed));
    b.append(top,meta);box.append(b);
  });
}
function gchip(g){const c=el("span","chip");const box=el("span","g "+(g.passed?"ok":"no"),g.passed?"✓":"✗");
  c.append(box, el("span",null,g.gate));return c}
function callRow(c){const d=el("div","call");
  d.append(el("b",null,c.role), el("span",null,(c.events||0)+" ev"), el("span",null,money(c.cost)));
  if(c.timed_out)d.append(el("span","to","↻ resumed"));return d}
function phaseCard(p){
  const card=el("div","phase");
  const head=el("div","ph");
  head.append(el("span","badge "+(p.kind==="agent"?"b-agent":"b-code"),p.kind),
              el("span","nm",p.name), el("span","own",p.owner||""));
  const dot=el("span","sdot "+(p.status==="success"?"d-ok":p.status==="running"?"d-run":"d-fail"));head.append(dot);
  card.append(head);
  if(p.items&&p.items.length){const it=el("div","items");
    p.items.forEach(i=>{ if(i.kind==="gate")it.append(gchip(i)); else it.append(callRow(i)); });
    card.append(it);}
  return card;
}
function marker(m){
  const map={finish:"verdict",verdict:"tally",review_verdict:"review",permission_breach:"write breach",
    canon_gap:"canon gap",converged:"converged",not_converged:"did not converge"};
  const d=el("div","marker"+(m.detail==="finish"?" final":""));
  const lbl=el("div","lbl",map[m.detail]||m.detail);
  d.append(lbl);
  const data=m.data||{};
  if(m.detail==="finish"){d.append(el("div",null,"<b>"+(data.accepted?"ACCEPTED":"not accepted")+"</b>"+
    (data.merged?" · merged to base":"")+" · "+money(data.cost_usd)));
    if(data.reason)d.append(el("div","ev",data.reason));}
  else if(m.detail==="review_verdict"){d.append(el("div",null,"approved: <b>"+data.approved+"</b>"));
    if((data.blocking||[]).length)d.append(el("div","ev","blocking: "+data.blocking.join(" · ")));}
  else {const s=JSON.stringify(data);if(s!=="{}")d.append(el("div","ev",s.slice(0,400)));}
  return d;
}
function loadDetail(id){
  fetch("/api/runs/"+id).then(r=>r.json()).then(run=>{
    if(sel!==id)return;
    const d=$("#detail");d.innerHTML="";
    const head=el("div","dhead");
    head.append(el("h1",null,run.target||run.run_id));
    head.append(el("span","lane",run.lane||""));
    const kv=el("div","kv");
    const [,lbl]=runStatus(run);
    kv.innerHTML="<span>status <b>"+lbl+"</b></span><span>cost <b>"+money(run.cost_usd)+"</b></span>"+
      "<span>branch <b>"+(run.work_branch? run.work_branch.replace('factory/','…/') :"—")+"</b></span>";
    head.append(kv);d.append(head);
    if(run.reason)d.append(el("div","reason",run.reason));
    const tl=el("div","tl");
    (run.timeline||[]).forEach(t=>{
      if(t.type==="phase")tl.append(phaseCard(t));
      else if(t.type==="iter")tl.append(el("div","iter","fix iteration "+t.i));
      else if(t.type==="gate")tl.append((()=>{const g=el("div","gate-solo");g.append(gchip(t));return g})());
      else if(t.type==="call")tl.append((()=>{const w=el("div","gate-solo");w.append(callRow(t));return w})());
      else if(t.type==="marker")tl.append(marker(t));
    });
    if(!tl.children.length)tl.append(el("div","empty","No phases recorded for this run."));
    d.append(tl);
  });
}
function poll(){fetch("/api/runs").then(r=>r.json()).then(d=>{runs=d.runs||[];renderList();if(sel)loadDetail(sel)}).catch(()=>{})}
poll();setInterval(poll,2000);
</script></body></html>"""


if __name__ == "__main__":
    main(sys.argv[1:])
