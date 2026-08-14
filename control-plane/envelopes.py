"""Typed handoff contracts. Every inter-agent handoff is one of these on disk (I2).

An envelope is a *claim* an agent returns; gates audit it (see gates.py). The producer
never validates its own claim. Envelopes are pydantic models so a parse failure re-prompts
the same OMP session rather than being coerced (the "agent proposes, code disposes" line).
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


# ----------------------------------------------------------------------------- envelopes
class EnvelopeBase(BaseModel):
    """The only required field is status; success must be earned (I5)."""
    status: Literal["success", "fail"]
    summary: str = ""
    notes_for_next_agent: str = ""

    model_config = {"extra": "allow"}  # roles may add fields; the subclass declares the ones gates read


class PlanOutput(EnvelopeBase):
    artifacts: list[str] = Field(default_factory=list)
    commit_message: str = ""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


class TestOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    unit_result: str = ""
    criteria_covered: list[str] = Field(default_factory=list)
    deferred_to_remote: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    criterion: str = ""
    severity: Literal["blocking", "major", "minor", "nit"] = "minor"
    confidence: Literal["high", "medium", "low"] = "medium"
    provenance: Literal["verified", "asserted", "unverified"] = "asserted"
    note: str = ""


class ReviewOutput(EnvelopeBase):
    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    notes_for_human: str = ""


class ArchitectFinding(BaseModel):
    model_config = {"populate_by_name": True, "extra": "allow"}
    ref: str = ""
    finding_class: Literal["technical", "product", "business"] = Field("technical", alias="class")
    disposition: Literal["resolved", "remediation_brief", "escalate"] = "escalate"
    action: str = ""
    authored: list[str] = Field(default_factory=list)   # protected-path files the architect wrote
    validation: str = ""                                # e.g. postgres validation result


class ArchitectOutput(EnvelopeBase):
    """The architect's triage of the reviewer's blocking findings: what it resolved against canon
    (never by weakening it), what the builder must still fix, and the one thing (if any) a human
    must decide. canon_integrity is its attestation that no test/invariant/AC was weakened to pass."""
    findings: list[ArchitectFinding] = Field(default_factory=list)
    remediation_brief: str = ""
    human_brief: dict = Field(default_factory=dict)
    canon_integrity: str = ""


ENVELOPE_TYPES: dict[str, type[EnvelopeBase]] = {
    "plan": PlanOutput,
    "build": BuildOutput,
    "test": TestOutput,
    "review": ReviewOutput,
    "architect": ArchitectOutput,
}


# ----------------------------------------------------------------------------- calls / phases / gates
class AgentCall(BaseModel):
    """One bounded OMP invocation. The control plane builds the flag set from this + config."""
    role: str                                  # planner|builder|test-author|reviewer -> model + tools + system.md
    output_type: str                           # key into ENVELOPE_TYPES
    prompt: str                                 # user message, piped via stdin (never positional — verified)
    add_dirs: list[str] = Field(default_factory=list)   # extra read-only dirs (e.g. the run dir with the plan)
    resume_session: str | None = None          # -r <id> for the correction/continuation loop
    max_time_s: int = 585                       # I9 wall-clock ceiling for this call


class PhaseParams(BaseModel):
    name: str
    kind: Literal["engineer", "agent", "code", "git"]
    owner: str
    description: str = Field(min_length=1)     # every phase earns a description (rejected if blank)
    writes: list[str] | None = None            # None = unrestricted; [] = read-only; [globs] = only those (I6)
    retries: int = 1


class GateReport(BaseModel):
    gate: str
    passed: bool
    checks: list[dict] = Field(default_factory=list)   # [{item, ok, note}]
    evidence: str = ""
