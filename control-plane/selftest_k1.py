#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""K1 self-test: prove the adapter spine against REAL P0 OMP streams (no new spend).

Verifies _parse_stream extracts the session id, the final-assistant envelope, and cost from
actual OMP JSONL, and that envelopes validate as their declared pydantic types."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import omp
import envelopes as E

RUN = HERE.parent / "runs" / "2026-08-07-p0-jrn-s1"
ok = True

def check(name, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

# 1. _json_slice tolerates prose / fences
print("json_slice:")
check("bare", omp._json_slice('{"status":"success"}') == '{"status":"success"}')
check("fenced", '{"a":1}' in omp._json_slice('here:\n```json\n{"a":1}\n```\n'))
check("prose-wrapped", omp._json_slice('done {"x":2} ok').strip() == '{"x":2}')

# 2. review stream -> ReviewOutput (clean bare JSON final message)
# The P0 recording lives under gitignored runs/, so it is present on the machine that made it and
# absent on a fresh clone (the VPS). Skip those sections there rather than crashing — the checks
# that ship their own fixtures must still run everywhere.
HAVE_P0 = (RUN / "review-phase.jsonl").exists() and (RUN / "build-fix2.jsonl").exists()
if not HAVE_P0:
    print(f"P0 recording absent ({RUN.name}) — skipping the replay-stream sections")
if HAVE_P0:
    print("review-phase.jsonl -> ReviewOutput:")
    lines = (RUN / "review-phase.jsonl").read_text().splitlines()
    sid, final, cost, n, perr, wait = omp._parse_stream(lines)
    check("session id present", bool(sid), sid or "")
    check("events counted", n > 100, f"{n} events")
    check("cost captured", cost > 0, f"${cost:.4f}")
    check("healthy stream flags no provider error", not perr and wait == 0.0)
    rv = E.ReviewOutput.model_validate_json(omp._json_slice(final))
    check("validates ReviewOutput", rv.status == "success")
    check("approved is bool", isinstance(rv.approved, bool), f"approved={rv.approved}")
    check("has blocking finding", any(f.severity == "blocking" for f in rv.findings),
          f"{len(rv.findings)} findings")

    # 3. build stream -> BuildOutput (final envelope after tool calls)
    print("build-fix2.jsonl -> BuildOutput:")
    bl = (RUN / "build-fix2.jsonl").read_text().splitlines()
    bsid, bfinal, bcost, bn, _bperr, _bwait = omp._parse_stream(bl)
    check("session id present", bool(bsid), bsid or "")
    if bfinal.strip().startswith("{") or "{" in bfinal:
        try:
            bo = E.BuildOutput.model_validate_json(omp._json_slice(bfinal))
            check("validates BuildOutput", bo.status in ("success", "fail"))
        except Exception as ex:
            check("validates BuildOutput", False, str(ex)[:80])
    else:
        check("build final present", False, "no JSON final (timed-out phase) — expected for some")

# 4. provider-failure classification: a rate limit / overload is INFRA, not a review finding.
#    Recorded from the 2026-08-18 outage, where a rate-limited reviewer was turned into the
#    synthetic finding "no review envelope" and drove three builder fix-iterations per journey.
print("provider-failure classification:")
FIX = HERE / "fixtures"
cases = [("codex-usage-limit.jsonl", "usage_limit_reached", 1800.0),
         ("anthropic-overloaded.jsonl", "overloaded_error", 0.0)]
for fname, needle, want_wait in cases:
    f = FIX / fname
    if not f.exists():
        check(f"{fname} fixture present", False, "missing")
        continue
    _s, fin, _c, _n, perr, wait = omp._parse_stream(f.read_text().splitlines())
    check(f"{fname}: provider error captured", needle in perr, perr[:60])
    check(f"{fname}: no envelope text", not fin)
    check(f"{fname}: advertised wait {want_wait}s", wait == want_wait, f"got {wait}")
    res = omp.AgentResult(ok=False, envelope=None, session_id=None, cost_usd=0.0, timed_out=False,
                          events=_n, raw_final=fin, error="", provider_error=perr, retry_after_s=wait)
    check(f"{fname}: infra_failed", res.infra_failed)
# a model that answered but garbled its envelope is NOT infra — it gets re-prompted, not backed off
behaved = omp.AgentResult(ok=False, envelope=None, session_id="s", cost_usd=0.0, timed_out=False,
                          events=9, raw_final="not json", error="envelope parse failed")
check("parse failure is not infra", not behaved.infra_failed)
# ...even when the stream ALSO carried a provider error that auto-retry absorbed (2026-08-19: a
# grok-4.6 stream did exactly this, and the run aborted instead of re-prompting).
recovered = omp.AgentResult(ok=False, envelope=None, session_id="s", cost_usd=0.1, timed_out=False,
                            events=900, raw_final="{not json", error="envelope parse failed",
                            provider_error="transient 500, retried")
check("answered-then-garbled is not infra", not recovered.infra_failed)
check("never-answered with a provider error IS infra",
      omp.AgentResult(ok=False, envelope=None, session_id=None, cost_usd=0.0, timed_out=False,
                      events=3, raw_final="", error="", provider_error="rate limited").infra_failed)

# A provider NOTICE delivered as assistant text is infra, not an answer (2026-10-02: a retired
# gemini-3-pro replied "no longer available" with stopReason=error and 0 output tokens; the chain
# stopped there, xAI was never reached, and probe() called the judge healthy). Shape-faithful
# minimal stream, hand-written.
import json as _json
def _stream(stop, out_tokens, text, err=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}],
           "usage": {"input": 0, "output": out_tokens, "cost": {"total": 0}}, "stopReason": stop}
    if err:
        msg["errorMessage"] = err
    return [_json.dumps({"type": "session", "id": "s-notice"}),
            _json.dumps({"type": "message_end", "message": msg}),
            _json.dumps({"type": "agent_end", "messages": [{"role": "user", "content": "go"}, msg]})]
NOTICE = "Gemini 3 Pro is no longer available. Please switch to Gemini 3.1 Pro."
_s, fin, _c, _n, perr, _w = omp._parse_stream(_stream("error", 0, NOTICE, "stream ended without a finish reason"))
check("retired-model notice yields no final answer", fin == "", fin[:60])
check("retired-model notice is captured as the provider error", "no longer available" in perr, perr[:80])
check("retired-model notice IS infra (chain walks on, probe reports down)",
      omp.AgentResult(ok=False, envelope=None, session_id=_s, cost_usd=0.0, timed_out=False,
                      events=_n, raw_final=fin, error="", provider_error=perr).infra_failed)
_s, fin, *_ = omp._parse_stream(_stream("error", 812, '{"status":"succ', "connection dropped"))
check("errored AFTER real output is still model output (not a notice)", fin.startswith('{"status"'))
_s, fin, _c, _n, perr, _w = omp._parse_stream(_stream("stop", 0, "not json"))
check("a normal stop with text is an answer, however garbled", fin == "not json" and not perr)
_noage = {"role": "assistant", "content": "x", "stopReason": "error"}
check("missing usage data is never classified as a notice", not omp._is_provider_notice(_noage))

# 5. the provider fallback chain: a paid second route to the same judgment (2026-08-18).
print("provider fallback chain:")
import config
check("paid fallbacks are OFF by default", not config.paid_fallback_enabled(),
      "an exhausted subscription pauses the factory; it does not spend")
check("with paid fallback off, every route is still FREE",
      all(not config.is_paid_route(m) for m in config.model_chain('reviewer')),
      " -> ".join(config.model_chain('reviewer')))
os.environ['OMP_ALLOW_PAID_FALLBACK'] = '1'
chain = config.model_chain('reviewer')
check("reviewer keeps its subscription model first", chain[0] == config.role('reviewer').model, chain[0])
check("reviewer has a fallback route", len(chain) > 1, " -> ".join(chain[1:]) or "none")
# A bare model name is fuzzy-matched across every authenticated provider, so any role with a PAID
# fallback must name its provider explicitly or the "free first" ordering is a fiction.
for _role in ('reviewer', 'test-author', 'product-manager'):
    _chain = config.model_chain(_role)
    check(f"{_role} primary names its provider", '/' in _chain[0], _chain[0])
    check(f"{_role} primary is the subscription", _chain[0].startswith('openai-codex/'), _chain[0])
    check(f"{_role} routes are distinct", len(set(_chain)) == len(_chain))
check("no fallback uses an UNAUTHORISED direct vendor API key",
      all(config.is_permitted_route(m) for m in chain[1:]),
      "subscriptions, OpenRouter, or an owner-authorised key (config.OWNER_AUTHORISED_DIRECT_KEYS)")
check("every METERED route is OpenRouter or owner-authorised",
      all(m.startswith('openrouter/') or m.startswith(config.OWNER_AUTHORISED_DIRECT_KEYS)
          for m in chain[1:] if config.is_paid_route(m)),
      "one account to turn off, unless the owner named the exception")
check("every metered route is spend-guarded",
      all(m.startswith('openrouter/')                      # balance floor, meter.allow_paid
          or m.split('/')[0] in ('xai',)                   # call budget, meter.allow_paid_call
          for m in chain[1:] if config.is_paid_route(m)),
      "a metered route with no ceiling is the 2026-08-19 overnight-billing failure")
check("a second family backs up the judge",
      any('openai' not in m and 'gpt' not in m for m in chain[1:]),
      chain[-1])
check("no retired model in the judge chain", 'google-antigravity/gemini-3-pro' not in chain,
      "retired 2026-10-02")
check("xAI is reachable in the judge chain when paid fallback is on",
      any(m.startswith('xai/') for m in chain))
check("no reroute lands on the builder's family",
      not any('claude' in m or 'anthropic' in m for m in chain),
      "reviewer never shares the builder's family (I3)")
check("builder is not rerouted", config.model_chain('builder') == (config.role('builder').model,))
import os
os.environ['OMP_FALLBACK_REVIEWER'] = ''
check("a fallback can be switched off by env", config.model_chain('reviewer') == (config.role('reviewer').model,))
os.environ['OMP_FALLBACK_REVIEWER'] = 'openrouter/x/y'
check("a fallback can be redirected by env", config.model_chain('reviewer')[-1] == 'openrouter/x/y')

# 6. every metered vendor in a judge chain gets its key forwarded (2026-10-02: xAI was authorised
#    and keyed in providers.env, but the key was never passed to omp — every xai/* call failed).
print("provider keys + pre-model failures:")
_vendor_key = {"openrouter": "OPENROUTER_API_KEY", "xai": "XAI_API_KEY"}
_saved_override = os.environ.pop('OMP_FALLBACK_REVIEWER', None)   # check the DEFAULT chain
os.environ['OMP_ALLOW_PAID_FALLBACK'] = '1'
check("reviewer default chain includes xAI", any(m.startswith('xai/') for m in config.model_chain('reviewer')))
for _role in ('reviewer', 'test-author', 'product-manager'):
    for _m in config.model_chain(_role):
        _p = _m.split('/')[0]
        if config.is_paid_route(_m) and _p in _vendor_key:
            check(f"{_role}: {_p} key is forwarded to omp", _vendor_key[_p] in omp._PROVIDER_KEY_PREFIXES)
os.environ.pop('OMP_ALLOW_PAID_FALLBACK', None)
if _saved_override is not None:
    os.environ['OMP_FALLBACK_REVIEWER'] = _saved_override
import tempfile
with tempfile.TemporaryDirectory() as _d:
    _f = Path(_d) / "providers.env"
    _f.write_text("XAI_API_KEY=test-not-a-key\nUNRELATED=x\n")
    os.environ['OMP_PROVIDER_ENV_FILE'] = str(_f)
    _env = {}
    omp._load_provider_keys(_env)
    check("XAI_API_KEY loads from providers.env", _env.get("XAI_API_KEY") == "test-not-a-key")
    check("unrelated variables are not loaded", "UNRELATED" not in _env)
    del os.environ['OMP_PROVIDER_ENV_FILE']

# omp failing BEFORE the model (no key) exits non-zero and explains only on stderr: that is a
# downed route, and must read as one in run() and probe() — not as "no complaint".
_nokey = "error: No API key found for xai.\n\nUse /login, set an API key environment variable"
check("a pre-model exit is a provider error", "No API key found for xai" in omp._exit_error(1, _nokey),
      omp._exit_error(1, _nokey))
check("a clean exit is not an error", omp._exit_error(0, "noise") == "")
check("a pre-model exit makes the result infra",
      omp.AgentResult(ok=False, envelope=None, session_id="s", cost_usd=0.0, timed_out=False, events=1,
                      raw_final="", error="", provider_error=omp._exit_error(1, _nokey)).infra_failed)
del os.environ['OMP_FALLBACK_REVIEWER']
os.environ.pop('OMP_ALLOW_PAID_FALLBACK', None)

print("\nK1 adapter-spine self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
