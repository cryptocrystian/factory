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


# OMP tool sets (verified names). Read-only = read,grep,glob; add write/edit/bash per role.
_RO = ("read", "grep", "glob")
_SRC = ("read", "write", "edit", "grep", "glob", "bash")     # builder: writes source + runs L0

ROLES: dict[str, Role] = {
    "planner": Role("planner", "claude-opus-5", "anthropic", "high",
                    _RO + ("write",), "planner/system.md", "plan"),
    "builder": Role("builder", "claude-opus-5", "anthropic", "xhigh",
                    _SRC, "builder/system.md", "build"),
    # test-author + reviewer: DIFFERENT family from the builder (I3, mandatory).
    "test-author": Role("test-author", "gpt-5.6-sol", "openai", "high",
                        _SRC, "test-author/system.md", "test"),
    "reviewer": Role("reviewer", "gpt-5.6-terra", "openai", "high",
                     _RO + ("bash",), "reviewer/system.md", "review"),
}

# Per-role repo write grant (I6), enforced post-hoc by permissions.py. None=unrestricted, []=read-only.
WRITE_GRANTS: dict[str, list[str] | None] = {
    "planner": [],                               # writes only its plan into the run dir, not the repo
    "builder": ["**"],                           # source; permissions.py additionally forbids protected paths
    "test-author": ["tests/**"],                 # tests only — the producer never writes tests it is judged by
    "reviewer": [],                              # writes nothing to the repo
}

# Paths no agent in a run may write (I12 + I3): factory machinery lives elsewhere; within a repo,
# canon and migrations and the test dir (for non-test-authors) are protected.
PROTECTED_PATHS = ("canon/**", "supabase/migrations/**", ".git/**")


@dataclass
class Budget:
    max_wall_s: int = 3600
    max_retries_per_phase: int = 2
    # token ceiling left open until tracer usage semantics are pinned (P0 finding)


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
