"""Authoritative paid-spend metering.

WHY NOT THE RUN LOGS. omp's per-call `usage.cost` under-reported OpenRouter by 20-70x on
2026-08-19 — it said $0.34 for work that actually billed $9.67. Anything built on that number is
fiction. The provider's own counter is the only source that can be trusted, so that is what this
reads, and it is what the budget rail and the per-journey figures are computed from.

Two jobs:
  1. A RAIL: refuse paid routes when the balance is below a floor, so a run cannot drain an account.
  2. A METER: what did this journey actually cost, so cost-per-LANDED-journey is a real number
     instead of an estimate. Spend with nothing to show for it is the failure mode; you cannot
     manage that without measuring the numerator and the denominator.

Fail-open on the rail, fail-quiet on the meter: a metering outage must never stop the factory,
and an unknown cost is reported as unknown rather than as zero.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEFAULT_MIN_BALANCE = 1.00        # refuse paid routes below this, in USD


def _api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    for cand in (os.environ.get("OMP_PROVIDER_ENV_FILE", ""),
                 str(Path.home() / ".omp" / "providers.env"),
                 "/root/.omp/providers.env"):
        if not cand:
            continue
        try:
            p = Path(cand)
            if not p.is_file():
                continue
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY=") and "=" in line:
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return None


def _get(url: str, timeout: float = 15.0) -> dict | None:
    key = _api_key()
    if not key:
        return None
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("data") or {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def usage_usd() -> float | None:
    """Total paid spend to date, from the provider. None when it cannot be read."""
    d = _get(OPENROUTER_KEY_URL)
    if d is None:
        return None
    try:
        return float(d.get("usage") or 0.0)
    except (TypeError, ValueError):
        return None


def balance_usd() -> float | None:
    """Credits remaining. None when it cannot be read."""
    d = _get(OPENROUTER_CREDITS_URL)
    if d is None:
        return None
    try:
        return float(d.get("total_credits") or 0.0) - float(d.get("total_usage") or 0.0)
    except (TypeError, ValueError):
        return None


def min_balance_usd() -> float:
    try:
        return float(os.environ.get("OMP_PAID_MIN_BALANCE_USD", DEFAULT_MIN_BALANCE))
    except ValueError:
        return DEFAULT_MIN_BALANCE


def allow_paid(balance_reader=balance_usd) -> tuple[bool, str]:
    """May a paid route be used right now? Fail-open: an unreadable balance does not block work,
    because a metering outage stopping production would be its own kind of waste."""
    bal = balance_reader()
    if bal is None:
        return True, "balance unknown — proceeding (metering is not a gate)"
    floor = min_balance_usd()
    if bal < floor:
        return False, f"paid balance ${bal:.2f} is below the ${floor:.2f} floor"
    return True, f"paid balance ${bal:.2f}"


class RunMeter:
    """Paid spend across one run: sample at the start, again at the end, report the delta."""

    def __init__(self, reader=usage_usd):
        self._reader = reader
        self.start: float | None = None
        self.end: float | None = None

    def open(self) -> "RunMeter":
        self.start = self._reader()
        return self

    def close(self) -> float | None:
        self.end = self._reader()
        if self.start is None or self.end is None:
            return None
        return max(0.0, round(self.end - self.start, 4))
