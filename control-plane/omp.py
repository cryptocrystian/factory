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
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import config
import envelopes as E


def _kill_tree(proc: subprocess.Popen) -> None:
    """Reap the entire process group, not just the direct child — a timed-out phase must not leave
    orphaned OMP/tool children alive to keep touching the workspace after the phase 'ended' (the
    root cause of the prior run interruption). Relies on start_new_session=True at spawn."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


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
    provider_error: str = ""       # the provider's own error (rate limit, overload) if it emitted one
    retry_after_s: float = 0.0     # the wait the provider asked for, in seconds (0 = none advertised)

    @property
    def infra_failed(self) -> bool:
        """No envelope AND the provider itself errored AND the model never answered at all.

        The last clause is load-bearing. A long agentic stream can carry a transient provider error
        that auto-retry absorbed, and still end with a final message; if that message's envelope
        merely fails to parse, the failure is BEHAVIOUR, not infrastructure, and re-prompting the
        same session is the right response. Without this, grok-4.6 emitting unparseable JSON on
        2026-08-19 was misread as a downed provider and aborted a run that should have re-prompted."""
        return (not self.ok) and bool(self.provider_error) and not self.raw_final


def _argv(call: E.AgentCall, r: config.Role, session_dir: Path, model: str | None = None) -> list[str]:
    argv = [
        config.OMP_BIN, "-p", "--mode", "json", "--no-title",
        "--model", model or r.model, "--thinking", r.thinking,
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
    """Return (session_id, final_assistant_text, cost_usd, n_events, provider_error, retry_after_s)."""
    session_id = None
    final_text = ""
    max_cost = 0.0
    n = 0
    provider_error = ""
    retry_after_s = 0.0
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
        # Provider-side failure (rate limit, overload): the model never answered. OMP reports it as a
        # message with stopReason "error", and — when the provider advertises a cooldown longer than
        # OMP's own retry ceiling — an auto_retry_end that names the wait it asked for. Both are worth
        # capturing: the first says "infra, not the model misbehaving", the second says how long.
        if isinstance(msg, dict) and msg.get("stopReason") == "error" and msg.get("errorMessage"):
            provider_error = str(msg["errorMessage"])
        if t == "auto_retry_end" and not ev.get("success"):
            fe = str(ev.get("finalError") or "")
            if fe:
                provider_error = provider_error or fe
            m = re.search(r"[Pp]rovider requested (\d+)ms wait", fe)
            if m:
                retry_after_s = max(retry_after_s, int(m.group(1)) / 1000.0)
        if t == "agent_end":
            for m in ev.get("messages", []):
                if m.get("role") == "assistant":
                    content = m.get("content")
                    txt = content if isinstance(content, str) else "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                    if txt.strip():
                        final_text = txt.strip()
    return session_id, final_text, max_cost, n, provider_error, retry_after_s


# Where a host advertises the auth-broker to borrow OAuth from. A broker-env file holds
# OMP_AUTH_BROKER_URL + OMP_AUTH_BROKER_TOKEN; loading it makes every omp child a *borrower*
# that never refreshes the raw OAuth itself. This is the single-refresher discipline: the broker
# service is the ONLY process that holds and rotates the credential, so no two holders can race
# and mutually invalidate each other (the failure mode that killed Anthropic auth on 2026-08-13).
# Hosts without a broker file (e.g. a dev laptop with its own vault) are unaffected — nothing loads.
def _broker_env_candidates() -> tuple[str, ...]:
    # Resolved per call (not frozen at import) so an OMP_BROKER_ENV_FILE override is honored.
    return (
        os.environ.get("OMP_BROKER_ENV_FILE", ""),
        str(Path.home() / ".omp" / "broker-client.env"),
        "/root/.omp/broker-client.env",
    )


# Pay-per-token provider keys (OpenRouter today) live beside the broker env, same discipline:
# a file on the box, never a repo, never an argv. They are the FALLBACK path — the subscription
# is always tried first, so nothing here is spent while quota is healthy.
_PROVIDER_KEY_PREFIXES = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")


def _provider_env_candidates() -> tuple[str, ...]:
    return (
        os.environ.get("OMP_PROVIDER_ENV_FILE", ""),
        str(Path.home() / ".omp" / "providers.env"),
        "/root/.omp/providers.env",
    )


def _load_provider_keys(env: dict[str, str]) -> None:
    """Add any provider API keys the host holds, without overriding what the parent already set.
    Fail-open exactly like the broker loader: an unreadable file leaves the environment alone."""
    for cand in _provider_env_candidates():
        if not cand:
            continue
        try:
            p = Path(cand)
            if not p.is_file():
                continue
            text = p.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _PROVIDER_KEY_PREFIXES:
                env.setdefault(k, v.strip())
        break


def _child_env() -> dict[str, str]:
    """Env for spawned omp: inherit the parent, then ensure broker vars are present if a
    broker-env file exists and the parent hasn't already set them. Fail-open: any read/stat error
    (missing file, or a path we can't even stat as this user) leaves the environment untouched —
    the child falls back to whatever local auth it has."""
    env = dict(os.environ)
    _load_provider_keys(env)
    if env.get("OMP_AUTH_BROKER_URL"):
        return env                                  # parent already points at a broker; respect it
    for cand in _broker_env_candidates():
        if not cand:
            continue
        try:
            p = Path(cand)
            if not p.is_file():                     # is_file()/stat can raise on a foreign /root
                continue
            text = p.read_text()
        except OSError:
            continue                                # unreadable/unstattable → try the next candidate
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("OMP_AUTH_BROKER_"):
                env.setdefault(k, v.strip())
        break                                       # first readable candidate wins
    return env


def probe(role_name: str, timeout_s: int = 90) -> tuple[bool, float, str]:
    """Is this role's PROVIDER answering at all? One trivial completion, no system prompt, no tools.

    Not a judgment about the model — just whether the family is reachable. A rate-limited provider
    fails this instantly and for free, which is the point: the factory otherwise spends a full build
    (planner + builder + test-author, the expensive family) before discovering that the cross-family
    reviewer cannot judge it, and everything built is unusable until the judge comes back.

    Fail-open by construction: only an explicit provider error reports unavailable. A probe that
    times out, crashes, or answers oddly reports AVAILABLE, so a broken check can never park the
    queue — the worst case is the old behavior."""
    r = config.role(role_name)
    worst_wait, worst_err = 0.0, ""
    # The role is available if ANY model in its chain answers: the subscription first (free at the
    # margin), then the paid fallback. Reporting the role down while a fallback is live would idle
    # the factory for no reason.
    for model in config.model_chain(role_name):
        argv = [config.OMP_BIN, "-p", "--mode", "json", "--no-title", "--model", model]
        try:
            proc = subprocess.run(argv, input="Reply with the single word OK.", capture_output=True,
                                  text=True, timeout=timeout_s, env=_child_env())
        except Exception:
            return True, 0.0, ""               # a broken probe never parks the queue
        _sid, final, _cost, _n, provider_error, retry_after_s = _parse_stream(proc.stdout.splitlines())
        if final:                              # it answered — up, whatever else the stream said
            return True, 0.0, ""
        if not provider_error:
            return True, 0.0, ""               # no answer, no provider complaint → not our call
        worst_wait = max(worst_wait, retry_after_s)
        worst_err = worst_err or f"{model}: {provider_error}"
    return False, worst_wait, worst_err


def run(call: E.AgentCall, run_dir: Path, workspace: Path, tracer=None,
        model: str | None = None) -> AgentResult:
    """Invoke one bounded OMP phase. `workspace` is the repo the agent operates in (its cwd).

    `model` overrides the role's primary model — used for the fallback chain, so a rate-limited
    subscription can hand the same role, same system prompt and same tools to a paid provider."""
    r = config.role(call.role)
    session_dir = run_dir / "sessions" / call.role
    session_dir.mkdir(parents=True, exist_ok=True)
    argv = _argv(call, r, session_dir, model)

    timed_out = False
    # Spawn in a new session so the whole process tree can be reaped on timeout (start_new_session).
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(workspace), start_new_session=True, env=_child_env(),
    )
    try:
        out, _ = proc.communicate(input=call.prompt, timeout=r.timeout_s)   # per-role budget (I9)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)                       # kill the group, not just the child
        try:
            out, _ = proc.communicate(timeout=15)   # drain whatever was buffered before the kill
        except Exception:
            out = ""

    lines = out.splitlines()
    suffix = "" if model in (None, r.model) else f".{model.replace('/', '_')}"
    (run_dir / f"{call.role}{suffix}.jsonl").write_text(out)
    session_id, final_text, cost, n, provider_error, retry_after_s = _parse_stream(lines)
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
        err = (f"provider failure: {provider_error}" if provider_error else
               "no final assistant message (timed out mid-turn?)" if timed_out else "no envelope emitted")

    return AgentResult(
        ok=envelope is not None, envelope=envelope, session_id=session_id,
        cost_usd=cost, timed_out=timed_out, events=n, raw_final=final_text, error=err,
        provider_error=provider_error, retry_after_s=retry_after_s,
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
