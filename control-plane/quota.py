#!/usr/bin/env python3
"""quota.py — read the subscription meters, and attribute what the FACTORY drew.

`omp usage` already reports plan-wide headroom and keeps hourly history, so recording that again
would be waste. The gap it cannot close is attribution: the Anthropic 7-day bar is shared by the
factory, other ventures, and the owner's own sessions, so "42% used" answers nothing about whether
the factory is worth its quota. This module samples the meters around a run and records the delta,
which turns that into a number.

That matters most for the metered-subscription models (see config.METERED_SUBSCRIPTION_MODELS).
Fable 5 is reported on its OWN 7-day bar, separate from the main Claude bar. Whether heavy Fable use
also moves the main bar is not documented and not inferable from a single snapshot — it is an
empirical question, and sampling both bars around a trial is what answers it. Until it is answered,
treat the two as possibly coupled.

Usage:
    python3 quota.py                 # headroom report
    python3 quota.py --json          # raw snapshot
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

# The factory runs on the host that owns the credential vault, so omp is local there. A workstation
# reading a remote factory's quota should ssh and run this script on the VPS rather than proxying.
OMP_BIN = os.environ.get("OMP_BIN") or shutil.which("omp") or "/usr/local/bin/omp"

FABLE_HINT = "fable"


@dataclass(frozen=True)
class Meter:
    provider: str
    label: str
    used_fraction: float          # 0.0 - 1.0
    resets_in_s: float | None

    @property
    def is_fable(self) -> bool:
        return FABLE_HINT in self.label.lower()

    @property
    def exhausted(self) -> bool:
        return self.used_fraction >= 0.999

    def bar(self, width: int = 24) -> str:
        filled = int(round(self.used_fraction * width))
        return "█" * filled + "░" * (width - filled)


def snapshot(timeout_s: int = 60) -> list[Meter]:
    """Current meters for every authenticated account. Returns [] if omp cannot be reached —
    quota reporting is observability, and must never be able to stop the factory."""
    try:
        proc = subprocess.run([OMP_BIN, "usage", "--json"], capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    now_ms = payload.get("generatedAt") or 0
    out: list[Meter] = []
    for report in payload.get("reports", []) or []:
        provider = report.get("provider", "?")
        for lim in report.get("limits", []) or []:
            amount = lim.get("amount") or {}
            frac = amount.get("usedFraction")
            if frac is None:
                used, limit = amount.get("used"), amount.get("limit")
                frac = (used / limit) if (isinstance(used, (int, float)) and limit) else None
            if frac is None:
                continue
            resets_at = (lim.get("window") or {}).get("resetsAt")
            resets_in = ((resets_at - now_ms) / 1000.0) if (resets_at and now_ms) else None
            out.append(Meter(provider=provider,
                             label=str(lim.get("label") or lim.get("id") or "?"),
                             used_fraction=float(frac),
                             resets_in_s=resets_in))
    return out


def key(m: Meter) -> str:
    return f"{m.provider}:{m.label}"


def as_dict(meters) -> dict[str, float]:
    return {key(m): m.used_fraction for m in meters}


def delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    """Fraction-of-plan consumed between two snapshots, per meter. Negative values mean the window
    reset mid-run and are dropped: a reset is not consumption, and reporting it as -38% would make
    a run look like it refunded quota."""
    out = {}
    for k, post in after.items():
        pre = before.get(k)
        if pre is None:
            continue
        moved = post - pre
        if moved > 0:
            out[k] = round(moved, 4)
    return out


def exhausted_providers(meters) -> list[str]:
    """Providers with at least one fully-spent window — a judge family here cannot certify a build."""
    return sorted({m.provider for m in meters if m.exhausted})


def _fmt_reset(s: float | None) -> str:
    if not s or s <= 0:
        return ""
    h = int(s // 3600)
    return f" · resets in {h // 24}d{h % 24}h" if h >= 24 else f" · resets in {h}h{int((s % 3600) // 60)}m"


def report(meters=None) -> str:
    meters = snapshot() if meters is None else meters
    if not meters:
        return "quota: unavailable (omp usage could not be read)"
    lines, current = [], None
    for m in sorted(meters, key=lambda x: (x.provider, x.label)):
        if m.provider != current:
            current = m.provider
            lines.append(f"\n{current}")
        flag = "  ← METERED (own ceiling, billable overage)" if m.is_fable else ""
        flag = "  ← EXHAUSTED" if m.exhausted else flag
        lines.append(f"  {m.bar()}  {m.used_fraction * 100:5.1f}%  {m.label}{_fmt_reset(m.resets_in_s)}{flag}")
    spent = exhausted_providers(meters)
    if spent:
        lines.append(f"\nexhausted: {', '.join(spent)} — a judge family here cannot certify a build")
    return "\n".join(lines).lstrip("\n")


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(as_dict(snapshot()), indent=2))
    else:
        print(report())
