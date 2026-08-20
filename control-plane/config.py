"""Roster + paths. modelRoles honors Rev4 §12: building is capability-bound (strongest model);
judging is correlation-bound (DIFFERENT families for test-author and reviewer than the builder).
Both families verified on subscription (Anthropic + OpenAI/Codex)."""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field

FACTORY_ROOT = Path(__file__).resolve().parent.parent          # ~/factory
AGENTS_DIR = FACTORY_ROOT / "agents"
RUNS_DIR = FACTORY_ROOT / "runs"
OMP_BIN = os.environ.get("OMP_BIN", "omp")


@dataclass(frozen=True)
class Role:
    name: str
    model: str
    family: str
    thinking: str
    tools: tuple[str, ...]                       # OMP tool vocabulary (glob, not ls/find; strict validation)
    system_md: str                               # path under agents/<role>/system.md
    output_type: str
    timeout_s: int = 900                         # per-call OMP wall-clock ceiling (I9), tuned per role
    fallbacks: tuple[str, ...] = ()              # models tried, in order, when the primary's PROVIDER is down


# OMP tool sets (verified names). Read-only = read,grep,glob; add write/edit/bash per role.
_RO = ("read", "grep", "glob")
_SRC = ("read", "write", "edit", "grep", "glob", "bash")     # builder: writes source + runs L0

ROLES: dict[str, Role] = {
    "planner": Role("planner", "claude-opus-5", "anthropic", "high",
                    _RO + ("write",), "planner/system.md", "plan", timeout_s=900),
    # builder: capability-bound, but a full feature build needs a real single-call window. `xhigh`
    # thinking at a short timeout churned (never converged); `medium` + 25 min lets it finish in one call.
    "builder": Role("builder", "claude-opus-5", "anthropic", "medium",
                    _SRC, "builder/system.md", "build", timeout_s=1500),
    # test-author + reviewer: DIFFERENT family from the builder (I3, mandatory).
    # PROVIDER-QUALIFIED ON PURPOSE. A bare "gpt-5.6-terra" is fuzzy-matched, and once an
    # OPENAI_API_KEY exists on the host it resolves to the paid `openai` provider instead of the
    # `openai-codex` subscription — silently, with no error and no log line, so the factory would
    # bill per token while free quota sat unused. Naming the provider is what makes "subscription
    # first, paid only on failure" actually true.
    "test-author": Role("test-author", "openai-codex/gpt-5.6-sol", "openai", "high",
                        _SRC, "test-author/system.md", "test", timeout_s=900),
    "reviewer": Role("reviewer", "openai-codex/gpt-5.6-terra", "openai", "high",
                     _RO + ("bash",), "reviewer/system.md", "review", timeout_s=600),
    # architect: the technical authority. Resolves reviewer findings the builder can't (protected
    # paths — migrations, canon) so the factory self-governs; surfaces only genuine business/product
    # decisions. Anthropic family (capability-bound authoring) — gated by the openai reviewer and
    # cross-checked by the openai PM (I3: architect ≠ reviewer/PM family). Needs bash to validate
    # migrations against real Postgres in-loop.
    "architect": Role("architect", "claude-opus-5", "anthropic", "high",
                      _SRC, "architect/system.md", "architect", timeout_s=1800),
    # product manager: rules routine product decisions the architect routes to it, surfacing only
    # owner-level business forks. DIFFERENT family from the architect (cross-check).
    "product-manager": Role("product-manager", "openai-codex/gpt-5.6-sol", "openai", "high",
                            _RO + ("write",), "product-manager/system.md", "architect", timeout_s=1200),
}

# --------------------------------------------------------------------------- provider fallbacks
# WHY: every role that judges the build (test-author, reviewer) and the PM sit on ONE OpenAI Codex
# subscription, and on 2026-08-18 its weekly quota ran out — with Anthropic at 40% used, the factory
# still stopped dead, because a build nobody can review is worth nothing. The fix is not a second
# subscription but a paid, uncapped SECOND ROUTE to the same judgment: OpenRouter, billed per token.
#
# The subscription is always tried first, so this costs nothing while quota is healthy; the fallback
# is reached only when the primary's provider itself fails (a rate limit or an outage — never when
# the model merely answered badly). Ids are overridable per role by env var, because an aggregator's
# catalog moves faster than this file does.
#
# Cross-family independence (I3) is preserved BY MODEL, not by billing route: the reviewer's
# fallback is still an OpenAI-family model, just reached through OpenRouter rather than Codex.
# EVERY fallback goes through OpenRouter — the owner bought it precisely so the factory would not
# need a direct vendor API key, and no direct key is configured on the box. Order:
#
#   1. the Codex SUBSCRIPTION (openai-codex/…), always first and free at the margin. Provider-
#      qualified deliberately: a bare "gpt-5.6-terra" is fuzzy-matched across authenticated
#      providers, so a stray vendor key on the host would silently outrank the subscription.
#   2. the SAME MODEL through OpenRouter — identical judgment, paid per token, reached only when
#      the subscription's quota is genuinely spent.
#   3. a DIFFERENT FAMILY through OpenRouter (xAI Grok) — for when OpenAI is broadly unavailable
#      rather than just out of quota. Cheaper on output than the primary, and its independence from
#      BOTH the Anthropic builder and the OpenAI judge is a feature, not a compromise: I3 asks the
#      reviewer not to share the builder's family, and Grok shares neither.
#
# Ids are overridable per role by env var, because an aggregator's catalog moves faster than this
# file does.
_FALLBACK_DEFAULTS: dict[str, tuple[str, ...]] = {
    "test-author":     ("openrouter/openai/gpt-5.6-sol",   "openrouter/x-ai/grok-4.6"),
    "reviewer":        ("openrouter/openai/gpt-5.6-terra", "openrouter/x-ai/grok-4.6"),
    "product-manager": ("openrouter/openai/gpt-5.6-sol",   "openrouter/x-ai/grok-4.6"),
}


def paid_fallback_enabled() -> bool:
    """Paid routes are OFF unless the owner switches them on (OMP_ALLOW_PAID_FALLBACK=1).

    The factory runs on subscriptions, where a run costs nothing at the margin. A fallback bills
    real money per token, and on 2026-08-19 it did so unattended and unbudgeted while the owner
    was asleep — which is not a decision software should make for someone. Off by default means an
    exhausted subscription PAUSES the factory (the preflight already holds dispatch and re-probes
    every 30 minutes, so it resumes by itself when quota returns) instead of quietly spending."""
    return os.environ.get("OMP_ALLOW_PAID_FALLBACK", "").strip() in ("1", "true", "yes", "on")


def _fallbacks_for(role_name: str) -> tuple[str, ...]:
    """Env override wins: OMP_FALLBACK_<ROLE> ("" disables the fallback, a comma-list replaces it).
    An explicit per-role override is honored even with paid fallbacks off, so a deliberate
    "use this route right now" still works without flipping the global switch."""
    key = "OMP_FALLBACK_" + role_name.upper().replace("-", "_")
    raw = os.environ.get(key)
    if raw is not None:
        return tuple(m.strip() for m in raw.split(",") if m.strip())
    if not paid_fallback_enabled():
        return ()
    return _FALLBACK_DEFAULTS.get(role_name, ())


def model_chain(role_name: str) -> tuple[str, ...]:
    """The models to try for a role, primary first. One entry unless a fallback is configured."""
    r = role(role_name)
    return (r.model,) + tuple(m for m in _fallbacks_for(role_name) if m and m != r.model)


# Per-role repo write grant (I6), enforced post-hoc by permissions.py. None=unrestricted, []=read-only.
WRITE_GRANTS: dict[str, list[str] | None] = {
    "planner": [],                               # writes only its plan into the run dir, not the repo
    "builder": ["**"],                           # source; permissions.py additionally forbids protected paths
    "test-author": ["tests/**"],                 # tests only — the producer never writes tests it is judged by
    "reviewer": [],                              # writes nothing to the repo
    # The architect is the ONLY role granted the protected paths (migrations + canon) — the authority
    # the builder lacks. Named explicitly (not "**"), so permissions.py permits these protected paths
    # for it alone. It does NOT get app source: app-logic fixes go back to the builder as a remediation
    # brief, keeping author/verifier separation intact.
    "architect": ["supabase/migrations/**", "canon/**"],
    "product-manager": ["canon/**"],             # rules routine product decisions into canon (AC/DEC)
}

# Paths no agent in a run may write (I12 + I3): factory machinery lives elsewhere; within a repo,
# canon and migrations and the test dir (for non-test-authors) are protected.
PROTECTED_PATHS = ("canon/**", "supabase/migrations/**", ".git/**")


@dataclass
class Budget:
    max_wall_s: int = 2700                        # per-phase wall cap across resumes — stop churning, escalate
    max_retries_per_phase: int = 2
    # token ceiling left open until tracer usage semantics are pinned (P0 finding)


# SSSF hard rule 7: every phase earns a description — one sentence on what it does and WHY, never
# a restatement of the name. It is the only intent the trace, console and observatory ever show, and
# ours said "planner phase" / "builder phase" for every phase until 2026-08-20, which is precisely
# the anti-pattern the rule rejects.
PHASE_INTENT: dict[str, str] = {
    "planner": "Turn the journey's governing canon into a plan the builder can implement without asking questions",
    "builder": "Implement the plan against canon and report every file it changed",
    "test-author": "Author the acceptance tests the build must satisfy — a different family from the builder, so the producer never sets its own bar",
    "reviewer": "Judge independently whether what was built is what canon actually asked for",
    "architect": "Resolve what the builder cannot: author the migration or canonical decision that makes the build satisfy canon",
    "product-manager": "Rule the product question the architect surfaced, or confirm it belongs to the owner",
}


def phase_intent(name: str) -> str:
    return PHASE_INTENT.get(name) or f"Run the {name} phase"


def validate_roles(names) -> None:
    """SSSF hard rule 1: validate before running. A misnamed or missing role must fail BEFORE
    anything spawns — otherwise the run discovers it three phases and several dollars deep."""
    missing = [n for n in names if n not in ROLES]
    if missing:
        raise KeyError(f"unknown role(s) {missing}; roster has {sorted(ROLES)}")


def role(name: str) -> Role:
    if name not in ROLES:
        raise KeyError(f"unknown role {name!r}; known: {list(ROLES)}")
    return ROLES[name]


# --------------------------------------------------------------------------- design (anti-slop) opt-in
@dataclass(frozen=True)
class DesignPolicy:
    """A repo's opt-in to the Impeccable anti-slop gate. Absent (None) = the gate never runs."""
    detect_paths: str                            # dirs the detector scans (missing ones are skipped)
    impeccable_version: str                      # pinned CLI version, "" = unpinned


def design_policy(repo_path) -> "DesignPolicy | None":
    """Read this repo's design-gate opt-in from registry.yml. Returns None when the repo is absent,
    hasn't opted in (design.enabled falsy), or yaml is unavailable — in every case the gate is simply
    off, so the control plane degrades safely. Matched by resolved filesystem path, not repo name."""
    try:
        import yaml
    except Exception:
        return None
    reg = FACTORY_ROOT / "registry.yml"
    if not reg.exists():
        return None
    data = yaml.safe_load(reg.read_text()) or {}
    want = Path(repo_path).expanduser().resolve()
    for _name, spec in (data.get("repos") or {}).items():
        p = (spec or {}).get("path")
        if p and Path(p).expanduser().resolve() == want:
            d = (spec.get("design") or {})
            if not d.get("enabled"):
                return None
            return DesignPolicy(detect_paths=str(d.get("detect_paths", "app src components")),
                                impeccable_version=str(d.get("impeccable_version", "")))
    return None
