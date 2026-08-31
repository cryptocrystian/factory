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
from urllib.parse import urlparse, parse_qs

import yaml

FACTORY_ROOT = Path(__file__).resolve().parent
RUNS_DIR = FACTORY_ROOT / "runs"
DECISIONS = RUNS_DIR / "decisions.yml"
BACKLOG = FACTORY_ROOT / "backlog.yml"
CP = FACTORY_ROOT / "control-plane"
sys.path.insert(0, str(CP))
import obsdb
import statefile


# ----------------------------------------------------------------------------- drill-down
def _clip(v, n=600):
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= n else s[:n] + " …"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def parse_stream(path: Path) -> dict:
    """Parse one OMP session file (internal format: `message` + `custom` tool events) into a readable
    activity trail — what the agent said and did — plus the prompt it got and the envelope it returned."""
    activity, cost, session_id, envelope, prompt = [], 0.0, None, "", ""
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = e.get("type")
        if t == "session":
            session_id = session_id or e.get("id")
        elif t == "message":
            m = e.get("message") or {}
            role, txt = m.get("role"), _text_of(m.get("content"))
            u = m.get("usage") or {}
            if isinstance(u.get("cost"), dict):
                cost = max(cost, float(u["cost"].get("total", 0)))
            if role == "assistant" and txt.strip():
                activity.append({"kind": "say", "text": _clip(txt, 700)}); envelope = txt.strip()
            elif role == "user" and not prompt and txt.strip():
                prompt = _clip(txt, 600)
            elif role == "tool":                              # tool result → attach to the last tool call
                res = txt or _clip(m.get("content"), 400)
                for a in reversed(activity):
                    if a["kind"] == "tool" and not a.get("result"):
                        a["result"] = _clip(res, 400); break
        elif t == "custom" and e.get("customType") == "tool_execution_start":
            d = e.get("data") or {}
            activity.append({"kind": "tool", "name": d.get("toolName"),
                             "args": _clip(d.get("args", {}), 300), "result": "", "error": False})
    return {"activity": activity, "envelope": _clip(envelope, 1400), "cost": round(cost, 4),
            "session_id": session_id, "prompt": prompt,
            "tools": sum(1 for a in activity if a["kind"] == "tool")}


def role_config(role: str) -> dict:
    """The agent's core four — context, model, prompt, tools — plus its write boundary.

    SSSF's observability shows this per phase because it is what you tune: if you cannot see which
    model, which thinking level, which tools and which system prompt produced a result, you cannot
    improve it. Ours showed the user prompt and nothing else."""
    try:
        import config as _cfg
        r = _cfg.role(role)
    except Exception:
        return {}
    sysmd = _cfg.AGENTS_DIR / r.system_md
    out = {
        "model": r.model,
        "chain": list(_cfg.model_chain(role)),
        "family": r.family,
        "thinking": r.thinking,
        "tools": list(r.tools),
        "timeout_s": r.timeout_s,
        "output_type": r.output_type,
        "writes": _cfg.WRITE_GRANTS.get(role),
        "intent": _cfg.phase_intent(role),
        "system_prompt_path": str(sysmd),
    }
    try:
        out["system_prompt"] = sysmd.read_text()[:20000]
    except OSError:
        out["system_prompt"] = ""
    return out


def phase_stream(run_id: str, role: str, seq: int) -> dict | None:
    rd = RUNS_DIR / run_id
    sess = rd / "sessions" / role
    files = sorted(sess.glob("*.jsonl")) if sess.exists() else []
    path = files[seq] if 0 <= seq < len(files) else (rd / f"{role}.jsonl" if (rd / f"{role}.jsonl").exists() else None)
    if not path:
        return None
    out = parse_stream(path)
    out["config"] = role_config(role)
    out["route"] = path.name          # which model actually served it (fallback streams are suffixed)
    # attach the brief the agent worked from, if present
    for name in ("context.md", "plan.md"):
        p = rd / name
        if p.exists() and "context" not in out:
            out["context_present"] = True
    return out


# ----------------------------------------------------------------------------- decisions

FACTORY_ROOT = Path(__file__).resolve().parent
BACKLOG = FACTORY_ROOT / "backlog.yml"


def factory_state():
    """Everything the landing view needs, in ONE call: what is building, what is queued behind
    what, what needs a ruling, and how much provider headroom is left.

    The dashboard previously opened on "select a run to inspect" — a list with nothing selected,
    which shows the operator nothing about the factory as a whole. What matters at a glance is
    concurrency (are the lanes full?), the queue (what is blocked, on what?), and the decisions
    (what is waiting on me?)."""
    import yaml
    out = {"lanes": [], "queue": [], "decisions": [], "quota": [], "counts": {}}

    # A run killed mid-flight (daemon restart) never gets finished_at, so "no finish time" alone
    # would mark every abandoned run as live forever — 14 lanes on a 3-lane factory. Live means:
    # unfinished, AND the newest run for its target, AND the backlog still says that item is
    # in_progress. The backlog is the authority on what the daemon is actually running.
    import yaml as _yaml
    try:
        _bl = _yaml.safe_load(BACKLOG.read_text()) or {}
        _running = {i["id"].lower() for i in _bl.get("items", []) if i.get("status") == "in_progress"}
    except Exception:
        _running = set()

    conn = obsdb.connect()
    try:
        rows = obsdb.list_runs(conn, limit=60) if hasattr(obsdb, "list_runs") else []
        seen_targets = set()
        for r in rows:                                  # newest first
            if r.get("finished_at"):
                continue
            tgt = (r.get("target") or "").lower()
            if tgt not in _running or tgt in seen_targets:
                continue
            seen_targets.add(tgt)
            detail = obsdb.get_run(conn, r["run_id"])
            if not detail:
                continue
            st = structure_run(detail)          # same shape the run-detail route serves
            out["lanes"].append({
                "run_id": r["run_id"], "target": r.get("target"),
                "started_at": r.get("started_at"),
                "cost_usd": st.get("cost_usd"),
                "timeline": st.get("timeline") or [],
            })
    finally:
        conn.close()

    try:
        bl = yaml.safe_load(BACKLOG.read_text()) or {}
        items = bl.get("items", [])
        done = {i["id"] for i in items if i.get("status") in ("accepted", "superseded")}
        building = {l["target"].lower() for l in out["lanes"] if l.get("target")}
        for i in items:
            st = i.get("status")
            if st in ("accepted", "superseded"):
                continue
            unmet = [d for d in (i.get("depends_on") or []) if d not in done]
            out["queue"].append({
                "id": i["id"], "status": st, "note": i.get("note", ""),
                "waiting_on": unmet,
                "building": i["id"].lower() in building,
            })
        out["counts"] = {
            "building": len(out["lanes"]),
            "ready": sum(1 for q in out["queue"] if q["status"] == "ready" and not q["waiting_on"]),
            "blocked": sum(1 for q in out["queue"] if q["waiting_on"]),
            "escalated": sum(1 for q in out["queue"] if q["status"] == "escalated"),
            "done": len(done),
            "total": len(items),
        }
    except Exception as ex:
        out["queue_error"] = str(ex)

    try:
        d = load_decisions()
        out["decisions"] = [x for x in (d.get("decisions") or []) if x.get("status") == "open"]
    except Exception:
        pass

    try:
        sys.path.insert(0, str(FACTORY_ROOT / "control-plane"))
        import quota
        out["quota"] = [{"provider": m.provider, "label": m.label,
                         "used": round(m.used_fraction, 4), "exhausted": m.exhausted}
                        for m in quota.snapshot()]
    except Exception:
        pass
    return out


def load_decisions():
    if not DECISIONS.exists():
        return {"decisions": []}
    return yaml.safe_load(DECISIONS.read_text()) or {"decisions": []}


DECISIONS_HEADER = "# Decisions queue — escalations + ratifications awaiting the human.\n"


def save_decisions(data):
    """Whole-file write kept only for callers that already hold the current data; prefer
    `statefile.update` so a concurrent daemon write cannot lose a ruling."""
    statefile.update(DECISIONS, lambda _cur: data, header=DECISIONS_HEADER, default={"decisions": []})


def record_decision(did, action, note):
    """A human ruling. Both the ruling and the backlog advance are applied to what is ON DISK NOW,
    under a lock: the daemon rewrites the backlog every 20 seconds, and before 2026-08-21 an
    approval landing inside that window was silently erased."""
    """Record a human ruling: update the decision, and advance the backlog item when all its
    decisions are resolved (a fix/launch-gate approval sets the item ready; a reject leaves it)."""
    status = ("approved" if action and not action.lower().startswith(("reject", "defer"))
              else (action or "rejected").lower().split()[0])
    captured: dict = {}

    def _rule(cur):
        for d in cur.get("decisions", []):
            if d["id"] == did:
                d["status"] = status
                d["human_action"] = action
                d["human_note"] = note
                captured.update(d)
        return cur

    data = statefile.update(DECISIONS, _rule, header=DECISIONS_HEADER, default={"decisions": []})
    if not captured:
        return {"error": "unknown decision"}
    dec = captured
    # advance the backlog if every decision for this item is now approved
    bid = dec.get("backlog_id")
    if bid and BACKLOG.exists():
        siblings = [d for d in data["decisions"] if d.get("backlog_id") == bid]
        if all(s["status"] == "approved" for s in siblings):
            def _reopen(bl):
                for it in bl.get("items", []):
                    if it["id"] == bid and it.get("kind") != "decision":
                        it["status"] = "ready"   # re-open for the orchestrator once the ruling lands
                return bl

            statefile.update(BACKLOG, _reopen,
                             header="# Factory backlog — the orchestrator's work queue.\n",
                             default={"items": []})
    return {"ok": True, "decision": dec}


# ----------------------------------------------------------------------------- shaping
def structure_run(run: dict) -> dict:
    """Group the flat event stream into an ordered timeline of phases, gates and markers."""
    tl = []
    cur = None
    seq = {}
    for e in run["events"]:
        ev = e["event"]
        detail = json.loads(e["detail"]) if e.get("detail") else {}
        if ev == "phase_start":
            nm = e["phase"]
            k = seq.get(nm, 0); seq[nm] = k + 1        # per-role instance index → maps to its session file
            cur = {"type": "phase", "name": nm, "kind": e["kind"], "owner": e["owner"],
                   "ts": e["ts"], "items": [], "status": "running", "seq": k}
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
            if path == "/api/factory":
                return self._json(factory_state(), 200)
            if path == "/api/decisions":
                data = load_decisions()
                pending = [d for d in data["decisions"] if d.get("status") == "pending"]
                return self._json({"decisions": data["decisions"], "pending": len(pending)})
            if path == "/api/roles":
                import config as _cfg
                return self._json({"roles": {n: role_config(n) for n in _cfg.ROLES}})
            if path.startswith("/api/runs/") and path.endswith("/phase"):
                rid = path[len("/api/runs/"):-len("/phase")]
                q = parse_qs(urlparse(self.path).query)
                role = q.get("role", [""])[0]; seq = int(q.get("seq", ["0"])[0])
                st = phase_stream(rid, role, seq)
                return self._json(st) if st else self._json({"error": "no stream"}, 404)
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

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path.startswith("/api/decisions/"):
                did = path[len("/api/decisions/"):]
                res = record_decision(did, body.get("action", ""), body.get("note", ""))
                return self._json(res, 200 if res.get("ok") else 400)
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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Factory · Observatory</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Crect width=%2716%27 height=%2716%27 rx=%273%27 fill=%27%2312161c%27/%3E%3Crect x=%272.5%27 y=%274%27 width=%277%27 height=%272%27 rx=%271%27 fill=%27%236ea8d8%27/%3E%3Crect x=%272.5%27 y=%277%27 width=%2711%27 height=%272%27 rx=%271%27 fill=%27%235bb07f%27/%3E%3Crect x=%272.5%27 y=%2710%27 width=%275%27 height=%272%27 rx=%271%27 fill=%27%23e7a94b%27/%3E%3C/svg%3E">
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
.viewtabs{display:flex;gap:5px;margin:16px 0 4px}
.viewtabs button{font-family:var(--mono);font-size:11px;padding:5px 13px;border:1px solid var(--brd);background:var(--surf);color:var(--dim);border-radius:3px;cursor:pointer;letter-spacing:.04em}
.viewtabs button.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.flow{display:flex;flex-direction:column;gap:20px;padding:14px 0 50px}
.track .tlabel{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);margin-bottom:9px;display:flex;align-items:center;gap:9px}
.track .tlabel::after{content:"";flex:1;height:1px;background:var(--brd)}
.nodes{display:flex;align-items:stretch;overflow-x:auto;padding-bottom:8px}
.fnode{flex:none;min-width:106px;border:1px solid var(--brd);border-radius:7px;background:var(--surf);padding:10px 12px;display:flex;flex-direction:column;gap:5px;position:relative}
.fnode.agent{border-top:2px solid var(--accent)}
.fnode.code{border-top:2px solid var(--info)}
.fnode.gate{min-width:0;flex-direction:row;align-items:center;gap:8px;background:var(--surf2);padding:10px 12px}
.fnode.gate.ok{border-color:color-mix(in srgb,var(--pass) 55%,var(--brd))}
.fnode.gate.no{border-color:var(--fail);background:color-mix(in srgb,var(--fail) 14%,var(--surf))}
.fnode.verdict.ok{border-color:color-mix(in srgb,var(--pass) 60%,var(--brd))}
.fnode.verdict.no{border-color:var(--fail)}
.fnode.final{border:1.5px solid var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--surf))}
.fnode .fnt{font-family:var(--mono);font-size:9px;letter-spacing:.07em;text-transform:uppercase;color:var(--mute)}
.fnode .fnn{font-size:13px;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
.fnode .fns{font-family:var(--mono);font-size:10.5px;color:var(--dim);white-space:nowrap}
.fst{position:absolute;top:8px;right:9px;width:7px;height:7px;border-radius:50%}
.gicon{width:16px;height:16px;border-radius:4px;display:grid;place-items:center;font-size:10px;color:#fff;flex:none}
.gicon.ok{background:var(--pass)}.gicon.no{background:var(--fail)}
.farrow{flex:none;align-self:center;color:var(--mute);padding:0 4px;font-size:13px}
.to-badge{position:absolute;bottom:6px;right:9px;font-family:var(--mono);font-size:8.5px;color:var(--wait)}
.nav{display:flex;gap:4px;margin-left:6px}
.nav button{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:5px 12px;border:1px solid var(--brd);background:var(--surf);color:var(--dim);border-radius:3px;cursor:pointer}
.nav button.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.nav .badge{background:var(--bad);color:#fff;border-radius:9px;padding:0 6px;font-size:10px;margin-left:6px}
.fnode.agent,.phase .ph{cursor:pointer}
.fnode.agent:hover{border-color:var(--accent)}
.drawer{position:fixed;inset:0;background:color-mix(in srgb,#000 55%,transparent);z-index:20;display:flex;justify-content:flex-end}
.drawer-inner{width:min(680px,94vw);height:100%;background:var(--bg);border-left:1px solid var(--brd);overflow-y:auto;padding:20px 22px}
.drawer h3{margin:0 0 3px;font-size:17px}
.drawer .dh{display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--brd);padding-bottom:12px;margin-bottom:14px}
.drawer .x{margin-left:auto;background:var(--surf);border:1px solid var(--brd);color:var(--txt);border-radius:4px;cursor:pointer;font-size:16px;width:30px;height:30px}
.act{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--brd);font-size:12.5px}
.act .tag{font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:2px 6px;border-radius:2px;height:fit-content;flex:none}
.act.say .tag{background:color-mix(in srgb,var(--info) 22%,transparent);color:var(--txt)}
.act.tool .tag{background:color-mix(in srgb,var(--accent) 20%,transparent);color:var(--txt)}
.act.tool.err .tag{background:color-mix(in srgb,var(--bad) 24%,transparent)}
.act .body{color:var(--txt-dim);white-space:pre-wrap;word-break:break-word;min-width:0}
.act .nm{font-family:var(--mono);color:var(--txt);font-size:12px}
.act .res{font-family:var(--mono);font-size:11px;color:var(--txt-mute);margin-top:3px}
.env{margin-top:14px;padding:12px;border:1px solid var(--accent);border-radius:5px;background:var(--surf);font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;word-break:break-word}
.env .lbl{color:var(--accent);text-transform:uppercase;letter-spacing:.08em;font-size:10px;margin-bottom:6px;display:block}
.decisions{padding:22px clamp(16px,3vw,30px);overflow-y:auto;max-width:900px}
.dcard{border:1px solid var(--brd);border-radius:6px;background:var(--surfc,var(--surf));margin-bottom:16px;overflow:hidden}
.dcard.kind-fix{border-top:3px solid var(--bad)} .dcard.kind-launch-gate{border-top:3px solid var(--info)} .dcard.kind-ratify{border-top:3px solid var(--accent)}
.dcard.resolved{opacity:.55}
.dcard .dt{padding:14px 18px 8px}
.dcard .dt h3{margin:0;font-size:16px}
.dcard .kchip{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--txt-mute)}
.dsec{padding:6px 18px}
.dsec .l{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--txt-mute);margin-bottom:3px}
.dsec.copilot{background:color-mix(in srgb,var(--accent) 7%,transparent);border-left:2px solid var(--accent);margin:8px 0}
.dsec.copilot .l{color:var(--accent)}
.dsec p{margin:0 0 8px;font-size:13.5px;color:var(--txt-dim)}
.dacts{display:flex;gap:8px;flex-wrap:wrap;padding:12px 18px 16px;border-top:1px solid var(--brd);align-items:center}
.dbtn{font-family:var(--mono);font-size:12px;padding:7px 14px;border-radius:4px;border:1px solid var(--brd);cursor:pointer;background:var(--surf);color:var(--txt)}
.dbtn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.dstatus{font-family:var(--mono);font-size:11px;color:var(--good);margin-left:auto}
.lanes{margin:14px 0}
.axis{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);padding:0 0 4px 132px}
.lane-row{display:flex;align-items:center;gap:8px;margin:3px 0}
.lane-name{width:124px;flex:none;font-size:12px;color:var(--dim);text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lane-track{position:relative;flex:1;height:26px;background:var(--panel);border-radius:5px;
  border:1px solid var(--brd)}
.blk{position:absolute;top:3px;height:20px;border-radius:4px;cursor:pointer;overflow:hidden;
  display:flex;align-items:center;padding:0 6px;min-width:14px;transition:filter .12s}
.blk:hover{filter:brightness(1.25)}
.blk-l{font-size:10px;color:#08110c;font-weight:600;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.blk.ok{background:var(--ok)}
.blk.bad{background:var(--bad)}
.blk.run{background:var(--dim)}
.cfg{margin:10px 0;padding:10px 12px;background:var(--panel);border:1px solid var(--brd);border-radius:6px}
.cfg h4{margin:0 0 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
.cfg .row{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px}
.cfg .row b{color:var(--fg)}
.cfg pre{margin:8px 0 0;max-height:220px;overflow:auto;font-size:11px;line-height:1.45;
  background:var(--bg);padding:8px;border-radius:4px;border:1px solid var(--brd);white-space:pre-wrap}

/* --- Factory view: the landing. Concurrency, queue, and what needs a human, on one screen. ----
   Deliberately NOT a card grid. Each band is a different question ("what is running?", "what is
   waiting on me?", "what is blocked on what?"), so each gets a different shape rather than the
   same rounded box repeated -- uniform emphasis is the tell of a generated layout. */
.fv{padding:16px 20px 40px;max-width:1500px;margin:0 auto}
.fbar{display:flex;gap:0;align-items:stretch;border:1px solid var(--brd);border-radius:6px;
  overflow:hidden;margin-bottom:22px;background:var(--panel)}
.fstat{flex:1;padding:11px 16px;border-right:1px solid var(--brd)}
.fstat:last-child{border-right:0}
.fstat b{display:block;font-size:22px;line-height:1.1;color:var(--fg);font-variant-numeric:tabular-nums}
.fstat span{font-family:var(--mono);font-size:9.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--dim)}
.fstat.alert b{color:var(--accent)}
.fstat.alert span{color:var(--accent)}
.fsec{margin:0 0 26px}
.fsec>h3{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--dim);margin:0 0 10px;font-weight:600}
.fsec>h3 i{font-style:normal;color:var(--mute)}
/* swim lanes */
.swim{display:flex;flex-direction:column;gap:7px}
.slane{display:grid;grid-template-columns:92px 1fr 118px;align-items:center;gap:12px}
.sname{font-family:var(--mono);font-size:12px;color:var(--fg);font-weight:600;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.sname:hover{color:var(--accent)}
.strack{position:relative;height:30px;background:var(--bg);border:1px solid var(--brd);border-radius:4px;overflow:hidden}
.sblk{position:absolute;top:0;height:100%;display:flex;align-items:center;padding:0 7px;
  font-size:10px;font-weight:600;white-space:nowrap;overflow:hidden;border-right:1px solid rgba(0,0,0,.35);
  color:#0d1117;cursor:pointer}
.sblk:hover{filter:brightness(1.2)}
.sblk.running{animation:livepulse 1.8s ease-in-out infinite}
@keyframes livepulse{0%,100%{opacity:1}50%{opacity:.62}}
.sblk.failed{background:var(--bad)!important;color:#fff}
.smeta{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
.smeta b{color:var(--fg);font-weight:600}
.slegend{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;padding-left:104px}
.slegend span{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);
  display:flex;align-items:center;gap:5px}
.slegend i{width:9px;height:9px;border-radius:2px;display:inline-block}
/* needs-you band: the one thing that must not look like everything else */
.fdec{border-left:3px solid var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent);
  padding:14px 18px;border-radius:0 6px 6px 0;margin-bottom:10px}
.fdec .q{font-size:15px;color:var(--fg);margin:0 0 8px;line-height:1.45}
.fdec .w{font-size:12.5px;color:var(--txt-dim,var(--dim));margin:0 0 12px;line-height:1.5}
.fdec .opts{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.fdec .opt{display:flex;gap:9px;align-items:flex-start;font-size:12.5px;color:var(--dim);line-height:1.45}
.fdec .opt b{color:var(--accent);font-family:var(--mono);font-size:11px;flex:none;padding-top:1px}
.fdec .acts{display:flex;gap:7px;flex-wrap:wrap}
.fbtn{font-family:var(--mono);font-size:11.5px;padding:7px 13px;border-radius:4px;cursor:pointer;
  border:1px solid var(--brd);background:var(--surf);color:var(--txt,var(--fg))}
.fbtn.go{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.fbtn:hover{filter:brightness(1.15)}
/* queue: a dependency list, not boxes */
.fq{display:flex;flex-direction:column}
.fqrow{display:grid;grid-template-columns:96px 84px 1fr;gap:12px;align-items:baseline;
  padding:6px 0;border-bottom:1px solid var(--brd);font-size:12.5px}
.fqrow:last-child{border-bottom:0}
.fqid{font-family:var(--mono);font-size:11.5px;color:var(--fg)}
.fqst{font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase}
.fqst.building{color:var(--ok)}
.fqst.blocked{color:var(--mute)}
.fqst.escalated{color:var(--accent)}
.fqst.ready{color:var(--info,var(--dim))}
.fqw{color:var(--dim)}
.fqw b{color:var(--fg);font-family:var(--mono);font-size:11px}
.fquota{display:flex;gap:16px;flex-wrap:wrap}
.fquota div{font-family:var(--mono);font-size:11px;color:var(--dim)}
.fquota b{color:var(--fg)}
.fquota .x{color:var(--bad)}
</style></head><body>
<header>
  <span class="brand">Factory · Observatory</span>
  <span class="nav" id="nav"></span>
  <span class="sub" id="count">—</span>
  <span class="live"><span class="pulse"></span> live · polling 2s</span>
</header>
<div class="fv-wrap" id="factoryview"></div>
<div class="layout" id="runsview" style="display:none">
  <div class="list" id="list"></div>
  <div class="detail" id="detail"><div class="empty">Select a run to inspect its pipeline — then click any agent phase to drill in.</div></div>
</div>
<div class="decisions" id="decisionsview" style="display:none"></div>
<div id="drawer"></div>
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
  if(p.kind==="agent")head.onclick=()=>openDrill(p.name,p.seq,p.name);
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
let view="lanes";
function renderTimeline(tlData){
  const tl=el("div","tl");
  (tlData||[]).forEach(t=>{
    if(t.type==="phase")tl.append(phaseCard(t));
    else if(t.type==="iter")tl.append(el("div","iter","fix iteration "+t.i));
    else if(t.type==="gate")tl.append((()=>{const g=el("div","gate-solo");g.append(gchip(t));return g})());
    else if(t.type==="call")tl.append((()=>{const w=el("div","gate-solo");w.append(callRow(t));return w})());
    else if(t.type==="marker")tl.append(marker(t));
  });
  if(!tl.children.length)tl.append(el("div","empty","No phases recorded for this run."));
  return tl;
}
// -- flow view: the assembly line. One track per attempt; nodes flow left->right through the gates.
function buildTracks(tlData){
  const tracks=[]; let cur={label:"run",nodes:[]}; tracks.push(cur);
  const P=n=>cur.nodes.push(n);
  (tlData||[]).forEach(t=>{
    if(t.type==="iter"){cur={label:"fix "+t.i,nodes:[]};tracks.push(cur)}
    else if(t.type==="phase"){
      const call=(t.items||[]).find(i=>i.kind==="call");
      P({c:t.kind==="agent"?"agent":"code",nt:t.kind,nn:t.name,role:t.name,seq:t.seq,
         ns:call?("$"+(call.cost||0).toFixed(2)):(t.owner||""),st:t.status,to:call&&call.timed_out});
      (t.items||[]).filter(i=>i.kind==="gate").forEach(g=>P({c:"gate",nn:g.gate,passed:g.passed}));
    }
    else if(t.type==="gate")P({c:"gate",nn:t.gate,passed:t.passed});
    else if(t.type==="call")P({c:"agent",nt:"agent",nn:t.role,ns:"$"+(t.cost||0).toFixed(2),to:t.timed_out});
    else if(t.type==="marker"){const dd=t.data||{};
      if(t.detail==="review_verdict")P({c:"verdict",nt:"review",nn:dd.approved?"approved":"rejected",passed:dd.approved});
      else if(t.detail==="finish")P({c:"final",nt:"verdict",nn:dd.accepted?"ACCEPTED":"not accepted",
        ns:(dd.merged?"merged · ":"")+"$"+(dd.cost_usd||0).toFixed(2),passed:dd.accepted});
    }
  });
  return tracks.filter(t=>t.nodes.length);
}
function fnode(n){
  if(n.c==="gate"){const g=el("div","fnode gate "+(n.passed?"ok":"no"));
    g.append(el("span","gicon "+(n.passed?"ok":"no"),n.passed?"✓":"✗"), el("span","fns",n.nn));return g}
  const d=el("div","fnode "+n.c+(n.c==="verdict"?(n.passed?" ok":" no"):""));
  if(n.nt)d.append(el("div","fnt",n.nt));
  d.append(el("div","fnn",n.nn));
  if(n.ns)d.append(el("div","fns",n.ns));
  if(n.st)d.append(el("span","fst "+(n.st==="success"?"d-ok":n.st==="running"?"d-run":"d-fail")));
  if(n.to)d.append(el("div","to-badge","↻ resumed"));
  if(n.c==="agent"&&n.role!=null)d.onclick=()=>openDrill(n.role,n.seq,n.nn);
  return d;
}
function renderFlow(tlData){
  const wrap=el("div","flow");
  buildTracks(tlData).forEach(tr=>{
    const t=el("div","track");
    t.append(el("div","tlabel",tr.label));
    const row=el("div","nodes");
    tr.nodes.forEach((n,i)=>{ if(i)row.append(el("span","farrow","→")); row.append(fnode(n)) });
    t.append(row); wrap.append(t);
  });
  if(!wrap.children.length)wrap.append(el("div","empty","No phases recorded for this run."));
  return wrap;
}
function renderLanes(tlData){
  // SSSF's signature view: one lane per agent, phases placed on a shared time axis, so a run reads
  // as WHO did WHAT and FOR HOW LONG rather than as a flat list. Code phases get their own lane —
  // the agents-plus-code split is the thing worth seeing at a glance.
  const ph=tlData.filter(x=>x.type==="phase"&&x.ts);
  if(!ph.length)return el("div","empty","No phases recorded for this run.");
  const t0=Math.min(...ph.map(p=>p.ts));
  const t1=Math.max(...ph.map(p=>p.ts_end||p.ts+1));
  const span=Math.max(1,t1-t0);
  const lanes=[];const byLane={};
  ph.forEach(p=>{
    const key=(p.kind==="code")?"· code":(p.owner||p.name);
    if(!byLane[key]){byLane[key]=[];lanes.push(key)}
    byLane[key].push(p);
  });
  const wrap=el("div","lanes");
  const axis=el("div","axis");
  axis.append(el("span",null,"0m"), el("span",null,Math.round(span/60)+"m"));
  wrap.append(axis);
  lanes.forEach(k=>{
    const lane=el("div","lane-row");
    lane.append(el("div","lane-name",k));
    const track=el("div","lane-track");
    byLane[k].forEach(p=>{
      const a=((p.ts-t0)/span)*100, w=Math.max(1.2,(((p.ts_end||p.ts+1)-p.ts)/span)*100);
      const cost=(p.items||[]).reduce((n,i)=>n+(i.cost||0),0);
      const b=el("div","blk "+(p.status==="success"?"ok":p.status==="fail"?"bad":"run"));
      b.style.left=a+"%";b.style.width=w+"%";
      const mins=Math.round(((p.ts_end||p.ts)-p.ts)/60);
      b.title=p.name+" · "+(p.status||"running")+" · "+mins+"m"+(cost?" · $"+cost.toFixed(2):"");
      b.append(el("span","blk-l",p.name));
      b.onclick=()=>openPhase(p);
      track.append(b);
    });
    lane.append(track);wrap.append(lane);
  });
  return wrap;
}
function openPhase(p){
  if(p.kind==="code"){return}
  showPhase(p.owner||p.name,p.seq||0);
}
function loadDetail(id){
  curRunId=id;
  fetch("/api/runs/"+id).then(r=>r.json()).then(run=>{
    if(sel!==id)return;
    const d=$("#detail");d.innerHTML="";
    const head=el("div","dhead");
    head.append(el("h1",null,run.target||run.run_id), el("span","lane",run.lane||""));
    const kv=el("div","kv");const [,lbl]=runStatus(run);
    kv.innerHTML="<span>status <b>"+lbl+"</b></span><span>cost <b>"+money(run.cost_usd)+"</b></span>"+
      "<span>branch <b>"+(run.work_branch?run.work_branch.replace('factory/','…/'):"—")+"</b></span>";
    head.append(kv);d.append(head);
    if(run.reason)d.append(el("div","reason",run.reason));
    const tabs=el("div","viewtabs");
    ["lanes","flow","timeline"].forEach(v=>{const b=el("button",view===v?"on":null,v);b.onclick=()=>{view=v;loadDetail(id)};tabs.append(b)});
    d.append(tabs);
    d.append(view==="lanes"?renderLanes(run.timeline)
            :view==="flow"?renderFlow(run.timeline)
            :renderTimeline(run.timeline));
  });
}
let curRunId=null, mode="factory", pending=0;
// -- drill-down drawer: what an agent actually said and did in one phase --
function openDrill(role,seq,label){
  if(!curRunId)return;
  fetch("/api/runs/"+curRunId+"/phase?role="+encodeURIComponent(role)+"&seq="+seq).then(r=>r.json()).then(st=>{
    const wrap=el("div","drawer");wrap.onclick=e=>{if(e.target===wrap)wrap.remove()};
    const inner=el("div","drawer-inner");
    const dh=el("div","dh");
    dh.append(el("span","badge b-agent","agent"), el("h3",null,label),
              el("span","fns","· "+(st.tools||0)+" tool calls · $"+(st.cost||0).toFixed(2)));
    const x=el("button","x","×");x.onclick=()=>wrap.remove();dh.append(x);
    inner.append(dh);
    // The core four — context, model, prompt, tools — plus the write boundary. Without these you
    // can see WHAT an agent did but not WHAT IT WAS, which is the half you actually tune.
    const c=st.config||{};
    if(c.model){
      const cfg=el("div","cfg");cfg.append(el("h4",null,"agent config"));
      const row=el("div","row");
      const chain=(c.chain||[]).join("  →  ")||c.model;
      row.innerHTML="<span>model <b>"+chain+"</b></span>"+
        "<span>thinking <b>"+(c.thinking||"—")+"</b></span>"+
        "<span>family <b>"+(c.family||"—")+"</b></span>"+
        "<span>timeout <b>"+(c.timeout_s||"—")+"s</b></span>"+
        "<span>output <b>"+(c.output_type||"—")+"</b></span>"+
        "<span>served by <b>"+(st.route||"—")+"</b></span>";
      cfg.append(row);
      const row2=el("div","row");
      row2.innerHTML="<span>tools <b>"+((c.tools||[]).join(", ")||"none")+"</b></span>";
      cfg.append(row2);
      const w=(c.writes===null||c.writes===undefined)?"unrestricted":((c.writes||[]).join(", ")||"read-only");
      const row3=el("div","row");row3.innerHTML="<span>writes <b>"+w+"</b></span>";cfg.append(row3);
      if(c.intent)cfg.append(el("div","row",c.intent));
      if(c.system_prompt){
        const det=document.createElement("details");
        const sum=document.createElement("summary");sum.textContent="system prompt";
        det.append(sum);const pre=el("pre",null,c.system_prompt);det.append(pre);cfg.append(det);
      }
      inner.append(cfg);
    }
    if(st.prompt){const pr=el("div","env");pr.append(el("span","lbl","user prompt (compiled)"));pr.append(document.createTextNode(st.prompt));pr.style.borderColor="var(--brd)";inner.append(pr)}
    (st.activity||[]).forEach(a=>{
      const row=el("div","act "+a.kind+(a.error?" err":""));
      if(a.kind==="tool"){row.append(el("span","tag",a.name||"tool"));
        const b=el("div","body");b.append(el("div","nm",a.args||""));
        if(a.result)b.append(el("div","res",a.result));row.append(b);}
      else {row.append(el("span","tag","says"), el("div","body",a.text||""));}
      inner.append(row);
    });
    if(st.envelope){const e=el("div","env");e.append(el("span","lbl","envelope returned"));e.append(document.createTextNode(st.envelope));inner.append(e);}
    if(!(st.activity||[]).length && !st.envelope)inner.append(el("div","empty","No stream recorded for this phase instance."));
    wrap.append(inner);$("#drawer").innerHTML="";$("#drawer").append(wrap);
  });
}
// -- decisions view: escalations + ratifications, with copilot analysis + approve controls --
function renderDecisions(list){
  const box=$("#decisionsview");box.innerHTML="";
  box.append(el("h2",null,"Decisions — your rulings, with copilot analysis"));
  if(!list.length){box.append(el("div","empty","No decisions pending. The line is clear."));return}
  list.forEach(d=>{
    const c=el("div","dcard kind-"+d.kind+(d.status!=="pending"?" resolved":""));
    const dt=el("div","dt");dt.append(el("div","kchip",d.kind+(d.run_id?(" · "+d.run_id):"")), el("h3",null,d.title));c.append(dt);
    const sec=(l,t,cls)=>{const s=el("div","dsec "+(cls||""));s.append(el("div","l",l));s.append(el("p",null,t));return s};
    if(d.finding)c.append(sec("Finding",d.finding));
    if(d.analysis)c.append(sec("Copilot analysis",d.analysis,"copilot"));
    if(d.recommendation)c.append(sec("Recommendation",d.recommendation,"copilot"));
    const acts=el("div","dacts");
    if(d.status==="pending"){
      (d.options||["Approve","Reject"]).forEach((opt,i)=>{
        const b=el("button","dbtn"+(i===0?" primary":""),opt);
        b.onclick=()=>{const note=prompt("Optional note for: "+opt)||"";
          fetch("/api/decisions/"+d.id,{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({action:opt,note})}).then(()=>pollDecisions())};
        acts.append(b);
      });
    } else {acts.append(el("span","dstatus","✓ "+(d.human_action||d.status)+(d.human_note?(" — "+d.human_note):"")))}
    c.append(acts);box.append(c);
  });
}
function pollDecisions(){return fetch("/api/decisions").then(r=>r.json()).then(d=>{
  pending=d.pending||0; renderNav();
  if(mode==="decisions")renderDecisions(d.decisions||[]);
}).catch(()=>{})}

const ROLECOLOR={planner:"#6ea8d8",builder:"#5bb07f","test-author":"#c8a2e0",reviewer:"#e7a94b",
  architect:"#e0655c","product-manager":"#4fbfa8",canon:"#6b7686",code:"#6b7686"};
function roleColor(o){return ROLECOLOR[o]||"#6b7686"}
function dur(a,b){const s=Math.max(0,(b-a));if(s<90)return Math.round(s)+"s";
  const m=s/60;return m<90?Math.round(m)+"m":(m/60).toFixed(1)+"h"}

function renderFactory(f){
  const box=$("#factoryview");box.innerHTML="";
  const wrap=el("div","fv");
  const c=f.counts||{};

  const bar=el("div","fbar");
  [["building",c.building,0],["ready",c.ready,0],["blocked",c.blocked,0],
   ["needs you",(f.decisions||[]).length,1],["shipped",(c.done||0)+"/"+(c.total||0),0]
  ].forEach(([lbl,v,alert])=>{
    const d=el("div","fstat"+(alert&&v?" alert":""));
    d.append(el("b",null,String(v==null?"-":v)),el("span",null,lbl));bar.append(d);
  });
  wrap.append(bar);

  // ---- swim lanes -------------------------------------------------------------------------
  const lanes=f.lanes||[];
  const s1=el("div","fsec");
  s1.append(el("h3",null,"In flight <i>&middot; live, shared time axis</i>"));
  if(!lanes.length){s1.append(el("div","empty","Nothing building. The daemon dispatches ready items on its next poll."))}
  else{
    const now=Date.now()/1000;
    let t0=Math.min(...lanes.map(l=>l.started_at||now));
    const span=Math.max(60,now-t0);
    const swim=el("div","swim");
    const used=new Set();
    lanes.forEach(l=>{
      const row=el("div","slane");
      const nm=el("div","sname",l.target||l.run_id);
      nm.onclick=()=>{mode="runs";sel=l.run_id;switchMode();loadDetail(l.run_id)};
      const tr=el("div","strack");
      let cost=0;
      (l.timeline||[]).forEach(ev=>{
        if(ev.type!=="phase")return;
        const st=ev.ts, en=ev.ts_end||now;
        const left=((st-t0)/span)*100, w=Math.max(0.9,((en-st)/span)*100);
        const b=el("div","sblk"+(ev.status==="running"?" running":"")+(ev.status==="failed"?" failed":""));
        b.style.left=left+"%";b.style.width=w+"%";
        if(ev.status!=="failed")b.style.background=roleColor(ev.owner);
        used.add(ev.owner);
        (ev.items||[]).forEach(it=>{cost+=(it.cost||0)});
        b.title=ev.name+" ("+ev.owner+") "+dur(st,en)+(ev.status?" - "+ev.status:"");
        if(w>7)b.append(document.createTextNode(ev.name));
        tr.append(b);
      });
      const meta=el("div","smeta");
      meta.innerHTML="<b>"+dur(l.started_at||now,now)+"</b> &middot; $"+cost.toFixed(2);
      row.append(nm,tr,meta);swim.append(row);
    });
    s1.append(swim);
    const lg=el("div","slegend");
    [...used].filter(Boolean).forEach(o=>{
      const sp=el("span");const i=el("i");i.style.background=roleColor(o);sp.append(i,document.createTextNode(o));lg.append(sp);
    });
    s1.append(lg);
  }
  wrap.append(s1);

  // ---- needs you --------------------------------------------------------------------------
  const ds=f.decisions||[];
  if(ds.length){
    const s2=el("div","fsec");
    s2.append(el("h3",null,"Needs you <i>&middot; "+ds.length+" waiting</i>"));
    ds.forEach(d=>{
      const card=el("div","fdec");
      card.append(el("p","q",d.finding||"(no question recorded)"));
      if(d.why)card.append(el("p","w",d.why));
      const opts=d.options||[];
      if(opts.length){
        const ob=el("div","opts");
        opts.forEach((o,i)=>{const r=el("div","opt");r.append(el("b",null,String(i+1)),el("span",null,o));ob.append(r)});
        card.append(ob);
      }
      const acts=el("div","acts");
      opts.forEach((o,i)=>{
        const b=el("button","fbtn"+(i===0?" go":""),"Approve "+(i+1));
        b.onclick=()=>{if(!confirm("Approve option "+(i+1)+"?\n\n"+o))return;
          fetch("/api/decisions/"+encodeURIComponent(d.id),{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({action:"approve option "+(i+1),note:o})}).then(()=>pollFactory())};
        acts.append(b);
      });
      const rj=el("button","fbtn","Reject");
      rj.onclick=()=>{const n=prompt("Why reject?");if(n==null)return;
        fetch("/api/decisions/"+encodeURIComponent(d.id),{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({action:"reject",note:n})}).then(()=>pollFactory())};
      acts.append(rj);
      card.append(acts);
      s2.append(card);
    });
    wrap.append(s2);
  }

  // ---- queue ------------------------------------------------------------------------------
  const q=(f.queue||[]).filter(x=>x.status!=="accepted");
  if(q.length){
    const s3=el("div","fsec");
    s3.append(el("h3",null,"Queue <i>&middot; what is waiting, and on what</i>"));
    const list=el("div","fq");
    q.forEach(x=>{
      const r=el("div","fqrow");
      const st=x.building?"building":x.status;
      r.append(el("div","fqid",x.id),el("div","fqst "+st,st));
      const w=el("div","fqw");
      w.innerHTML=x.waiting_on&&x.waiting_on.length
        ? "waiting on <b>"+x.waiting_on.join("</b>, <b>")+"</b>"
        : (x.note?String(x.note).slice(0,110):"");
      r.append(w);list.append(r);
    });
    s3.append(list);wrap.append(s3);
  }

  // ---- provider headroom -------------------------------------------------------------------
  const qa=f.quota||[];
  if(qa.length){
    const s4=el("div","fsec");
    s4.append(el("h3",null,"Provider headroom"));
    const g=el("div","fquota");
    qa.forEach(m=>{
      const d=el("div");
      d.innerHTML=m.provider+" &middot; "+m.label+" <b class='"+(m.exhausted?"x":"")+"'>"
        +Math.round(m.used*100)+"%</b>";
      g.append(d);
    });
    s4.append(g);wrap.append(s4);
  }

  box.append(wrap);
}
function pollFactory(){return fetch("/api/factory").then(r=>r.json()).then(f=>{
  factoryState=f; pending=(f.decisions||[]).length;
  if(mode==="factory")renderFactory(f); renderNav();
}).catch(()=>{})}

function renderNav(){
  const n=$("#nav");n.innerHTML="";
  [["factory","Factory"],["runs","Runs"],["decisions","Decisions"]].forEach(([m,lbl])=>{
    const b=el("button",mode===m?"on":null);b.append(document.createTextNode(lbl));
    if(m==="decisions"&&pending)b.append(el("span","badge",String(pending)));
    b.onclick=()=>{mode=m;switchMode()};n.append(b);
  });
}
function switchMode(){
  $("#factoryview").style.display = mode==="factory"?"block":"none";
  $("#runsview").style.display = mode==="runs"?"grid":"none";
  $("#decisionsview").style.display = mode==="decisions"?"block":"none";
  renderNav(); if(mode==="decisions")pollDecisions(); if(mode==="factory")pollFactory();
}
function poll(){fetch("/api/runs").then(r=>r.json()).then(d=>{runs=d.runs||[];renderList();if(sel&&mode==="runs")loadDetail(sel)}).catch(()=>{})}
let factoryState={};
renderNav();switchMode();poll();pollDecisions();pollFactory();
setInterval(()=>{poll();pollDecisions();pollFactory()},2000);
</script></body></html>"""


if __name__ == "__main__":
    main(sys.argv[1:])
