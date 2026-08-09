#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""K1 self-test: prove the adapter spine against REAL P0 OMP streams (no new spend).

Verifies _parse_stream extracts the session id, the final-assistant envelope, and cost from
actual OMP JSONL, and that envelopes validate as their declared pydantic types."""
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
print("review-phase.jsonl -> ReviewOutput:")
lines = (RUN / "review-phase.jsonl").read_text().splitlines()
sid, final, cost, n = omp._parse_stream(lines)
check("session id present", bool(sid), sid or "")
check("events counted", n > 100, f"{n} events")
check("cost captured", cost > 0, f"${cost:.4f}")
rv = E.ReviewOutput.model_validate_json(omp._json_slice(final))
check("validates ReviewOutput", rv.status == "success")
check("approved is bool", isinstance(rv.approved, bool), f"approved={rv.approved}")
check("has blocking finding", any(f.severity == "blocking" for f in rv.findings),
      f"{len(rv.findings)} findings")

# 3. build stream -> BuildOutput (final envelope after tool calls)
print("build-fix2.jsonl -> BuildOutput:")
bl = (RUN / "build-fix2.jsonl").read_text().splitlines()
bsid, bfinal, bcost, bn = omp._parse_stream(bl)
check("session id present", bool(bsid), bsid or "")
if bfinal.strip().startswith("{") or "{" in bfinal:
    try:
        bo = E.BuildOutput.model_validate_json(omp._json_slice(bfinal))
        check("validates BuildOutput", bo.status in ("success", "fail"))
    except Exception as ex:
        check("validates BuildOutput", False, str(ex)[:80])
else:
    check("build final present", False, "no JSON final (timed-out phase) — expected for some")

print("\nK1 adapter-spine self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
