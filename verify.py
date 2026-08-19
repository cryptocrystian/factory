#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2","pyyaml>=6"]
# ///
"""Verify the factory pipeline END TO END, spending nothing.

WHY THIS EXISTS. Every bug that cost real money on 2026-08-18/19 was one a pipeline check would
have caught: a rate-limited provider being turned into a code finding, a run aborting on a garbled
envelope, the replay harness itself crashing on an unmapped role. None of them needed a live model
to expose — but there was no single command that ran the pipeline and asserted the result, so they
shipped. This is that command.

    uv run verify.py [--repo PATH] [--with-docker]

Every check is free: the replay lane feeds RECORDED envelopes through the LIVE control plane, so
sequencing, envelope typing, gates, permissions and the fix loop are all exercised with zero model
calls. --with-docker adds the migration gate and the DEC-062 behavioural proof (real Postgres).

Exit 0 = the pipeline behaves as recorded. Nonzero = something changed; the summary names it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REC = ROOT / "runs" / "2026-08-07-p0-jrn-s1"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    return bool(ok)


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 900) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def latest_run_dir(target: str) -> Path | None:
    ds = sorted((d for d in (ROOT / "runs").glob(f"*-feature-{target}") if d.is_dir()),
                key=lambda d: d.name)
    return ds[-1] if ds else None


def verify_replay(repo: Path) -> None:
    """The golden regression: recorded envelopes through the live control plane."""
    if not REC.is_dir():
        check(False, "replay recording present", str(REC))
        return
    rc, out = run(["uv", "run", str(ROOT / "lanes" / "feature.py"),
                   "--repo", str(repo), "--journey", "JRN-S1", "--replay", str(REC)])
    check("Traceback" not in out, "replay lane runs without crashing", out.strip().splitlines()[-1][:90] if out else "")
    d = latest_run_dir("JRN-S1")
    if not d:
        check(False, "replay produced a run record")
        return
    ev = []
    for line in (d / "trace.jsonl").read_text().splitlines():
        try:
            ev.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    phases_ok = {e.get("phase") for e in ev if e.get("event") == "phase_end" and e.get("status") == "success"}
    for ph in ("resolve", "planner", "builder", "test-author", "reviewer"):
        check(ph in phases_ok, f"phase completes: {ph}")

    gates = {}
    for e in ev:
        if e.get("event") == "gate" and e.get("gate"):
            gates.setdefault(e["gate"], []).append(bool(e.get("passed")))
    # Gates that MUST hold in replay. diff_matches_claims is excluded on purpose: replay applies no
    # diffs, so it cannot pass and is advisory there by design.
    for g in ("artifacts_exist", "verdict_consistent", "typecheck", "check:tokens", "test:unit"):
        check(bool(gates.get(g)) and all(gates[g]), f"gate holds: {g}",
              "" if gates.get(g) else "gate never ran")

    details = [e.get("event_detail") for e in ev]
    check("review_verdict" in details, "reviewer produced a real verdict")
    check("agent_no_envelope" not in details, "no phase lost its envelope")
    check("permission_breach" not in details, "no role wrote outside its grant")
    check("run_aborted_unavailable" not in details, "no phase reported itself unavailable")
    check(details.count("fix_iter") >= 1, "the bounded fix loop engaged")
    fin = [e for e in ev if e.get("event_detail") == "finish"]
    check(bool(fin), "the run reached an explicit verdict")
    if fin:
        # The recorded P0 review carried blocking findings, so the golden outcome is NOT accepted.
        # Pinning it means a change that silently starts green-lighting this run fails the check.
        check(fin[-1].get("accepted") is False, "golden outcome preserved (recorded run: not accepted)",
              f"accepted={fin[-1].get('accepted')}")


def verify_config() -> None:
    sys.path.insert(0, str(ROOT / "control-plane"))
    import config  # noqa: E402

    check(not config.paid_fallback_enabled(), "paid fallbacks are off by default")
    for role in ("reviewer", "test-author", "product-manager"):
        chain = config.model_chain(role)
        check(chain == (config.role(role).model,), f"{role} runs subscription-only unless opted in",
              " → ".join(chain))
        check(chain[0].startswith("openai-codex/"), f"{role} primary is the subscription", chain[0])
    check(config.model_chain("builder")[0] == config.role("builder").model, "builder is not rerouted")
    # I3: whoever judges must not share the builder's family, on every configured route.
    import os
    os.environ["OMP_ALLOW_PAID_FALLBACK"] = "1"
    for role in ("reviewer", "test-author"):
        chain = config.model_chain(role)
        check(not any("claude" in m or "anthropic" in m for m in chain),
              f"{role} never shares the builder's family (I3)")
        check(all(m.startswith("openai-codex/") or m.startswith("openrouter/") for m in chain),
              f"{role} uses no direct vendor API", " → ".join(chain))
    os.environ.pop("OMP_ALLOW_PAID_FALLBACK", None)


def verify_selftest() -> None:
    rc, out = run(["uv", "run", str(ROOT / "control-plane" / "selftest_k1.py")])
    check(rc == 0 and "ALL PASS" in out, "K1 adapter-spine self-test",
          [l for l in out.splitlines() if "FAIL" in l][:1] or "")


def verify_docker(repo: Path) -> None:
    rc, out = run(["bash", str(ROOT / "control-plane" / "validate_migrations.sh")], cwd=repo, timeout=900)
    check(rc == 0, "every migration applies to a real Postgres",
          [l for l in out.splitlines() if "FAIL" in l][:1] or "")
    script = repo / "scripts" / "verify-stage-derivation.sh"
    if script.is_file():
        rc, out = run(["bash", str(script)], cwd=repo, timeout=900)
        check(rc == 0, "DEC-062 stage derivation behaves as ratified",
              [l for l in out.splitlines() if "FAIL" in l][:1] or "")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path.home() / "projects" / "arxus"))
    ap.add_argument("--with-docker", action="store_true", help="also run the real-Postgres gates")
    a = ap.parse_args(argv)
    repo = Path(a.repo).expanduser().resolve()

    print("factory verify — no model calls, no spend\n")
    verify_config()
    verify_selftest()
    verify_replay(repo)
    if a.with_docker:
        verify_docker(repo)

    print()
    failed = 0
    for ok, label, detail in results:
        if not ok:
            failed += 1
        d = f"  {detail}" if detail else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{d}")
    print(f"\n{len(results) - failed}/{len(results)} checks pass")
    if failed:
        print("PIPELINE NOT VERIFIED — the failures above are the pipeline, not the product.")
    else:
        print("PIPELINE VERIFIED — recorded envelopes flow through the live control plane as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
