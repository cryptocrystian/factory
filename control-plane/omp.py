"""The Agent port: (role, prompt, tools, model, dirs) -> typed envelope, over Oh My Pi.

Everything here is the verified adapter contract from FACTORY-HARNESS-SPEC.md:
  - prompt is piped via STDIN (never positional; -p reads stdin), with --no-title
  - --mode json emits JSONL; the envelope is the LAST assistant message in agent_end (bare JSON)
  - the session id is the first `session` event; -r <id> resumes the same session (correction loop)
  - OMP tool vocabulary is strict (glob, not ls/find); per-role allowlist from config
  - usage/cost is per-message; we take the max cumulative cost.total in the stream as the call cost
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import config
import envelopes as E


@dataclass
class AgentResult:
    ok: bool                       # process completed and the envelope parsed
    envelope: E.EnvelopeBase | None
    session_id: str | None
    cost_usd: float
    timed_out: bool
    events: int
    raw_final: str                 # the last assistant text (for diagnostics if parse failed)
    error: str = ""


def _argv(call: E.AgentCall, r: config.Role, session_dir: Path) -> list[str]:
    argv = [
        config.OMP_BIN, "-p", "--mode", "json", "--no-title",
        "--model", r.model, "--thinking", r.thinking,
        "--system-prompt", str(config.AGENTS_DIR / r.system_md),
        "--no-skills", "--no-rules", "--no-extensions", "--auto-approve",
        "--tools", ",".join(r.tools),
        "--session-dir", str(session_dir),
    ]
    for d in call.add_dirs:
        argv += ["--add-dir", str(d)]
    if call.resume_session:
        argv += ["-r", call.resume_session]
    return argv


def _parse_stream(lines: list[str]):
    """Return (session_id, final_assistant_text, cost_usd, n_events, emit(event))."""
    session_id = None
    final_text = ""
    max_cost = 0.0
    n = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        n += 1
        t = ev.get("type")
        if t == "session" and session_id is None:
            session_id = ev.get("id")
        # cost: per-message usage.cost.total is cumulative within the call; take the max seen
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        for m in ([msg] if msg else []) + (ev.get("messages") or []):
            u = (m or {}).get("usage") or {}
            c = u.get("cost")
            if isinstance(c, dict):
                max_cost = max(max_cost, float(c.get("total", 0.0)))
        if t == "agent_end":
            for m in ev.get("messages", []):
                if m.get("role") == "assistant":
                    content = m.get("content")
                    txt = content if isinstance(content, str) else "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                    if txt.strip():
                        final_text = txt.strip()
    return session_id, final_text, max_cost, n


def run(call: E.AgentCall, run_dir: Path, workspace: Path, tracer=None) -> AgentResult:
    """Invoke one bounded OMP phase. `workspace` is the repo the agent operates in (its cwd)."""
    r = config.role(call.role)
    session_dir = run_dir / "sessions" / call.role
    session_dir.mkdir(parents=True, exist_ok=True)
    argv = _argv(call, r, session_dir)

    timed_out = False
    try:
        proc = subprocess.run(
            argv, input=call.prompt, capture_output=True, text=True,
            timeout=call.max_time_s, cwd=str(workspace),
        )
        out = proc.stdout
    except subprocess.TimeoutExpired as te:
        timed_out = True
        out = te.stdout.decode() if isinstance(te.stdout, bytes) else (te.stdout or "")

    lines = out.splitlines()
    (run_dir / f"{call.role}.jsonl").write_text(out)
    session_id, final_text, cost, n = _parse_stream(lines)
    if tracer:
        tracer.record_call(call.role, n, cost, timed_out)

    etype = E.ENVELOPE_TYPES[call.output_type]
    envelope, err = None, ""
    if final_text:
        try:
            envelope = etype.model_validate_json(_json_slice(final_text))
        except Exception as ex:  # parse/validate failure -> caller re-prompts same session (bounded)
            err = f"envelope parse failed: {ex}"
    else:
        err = "no final assistant message (timed out mid-turn?)" if timed_out else "no envelope emitted"

    return AgentResult(
        ok=envelope is not None, envelope=envelope, session_id=session_id,
        cost_usd=cost, timed_out=timed_out, events=n, raw_final=final_text, error=err,
    )


def _json_slice(text: str) -> str:
    """Tolerate a fenced ```json block or surrounding prose; extract the JSON object."""
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            return m.group(1)
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if a != -1 and b != -1 else text
