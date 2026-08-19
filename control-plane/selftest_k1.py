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

# 5. the provider fallback chain: a paid second route to the same judgment (2026-08-18).
print("provider fallback chain:")
import config
check("paid fallbacks are OFF by default", not config.paid_fallback_enabled(),
      "an exhausted subscription pauses the factory; it does not spend")
check("with paid fallback off the chain is subscription-only",
      config.model_chain('reviewer') == (config.role('reviewer').model,))
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
check("no fallback uses a direct vendor API",
      all(m.startswith('openrouter/') for m in chain[1:]),
      "every paid route goes through OpenRouter")
check("a second family backs up the judge",
      any('openai' not in m and 'gpt' not in m for m in chain[1:]),
      chain[-1])
check("no reroute lands on the builder's family",
      not any('claude' in m or 'anthropic' in m for m in chain),
      "reviewer never shares the builder's family (I3)")
check("builder is not rerouted", config.model_chain('builder') == (config.role('builder').model,))
import os
os.environ['OMP_FALLBACK_REVIEWER'] = ''
check("a fallback can be switched off by env", config.model_chain('reviewer') == (config.role('reviewer').model,))
os.environ['OMP_FALLBACK_REVIEWER'] = 'openrouter/x/y'
check("a fallback can be redirected by env", config.model_chain('reviewer')[-1] == 'openrouter/x/y')
del os.environ['OMP_FALLBACK_REVIEWER']
os.environ.pop('OMP_ALLOW_PAID_FALLBACK', None)

print("\nK1 adapter-spine self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
