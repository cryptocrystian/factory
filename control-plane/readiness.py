"""Pre-dispatch readiness — decide whether a journey CAN succeed before spending a run on it.

WHY. JRN-B1 spent five hours and thirteen review cycles discovering, over and over, that
`0012_bqs.sql` and its RPCs did not exist. The reviewer was right every time; the run was doomed
from the first phase. Nothing checked, before dispatch, whether the substrate the acceptance
criteria name was actually present — so the factory paid a full journey to learn something a
thirty-second read of canon and the schema would have told it.

This is the front-end canon check: resolve the journey, then confirm the things it references
exist. What is missing is not a failure — it is ARCHITECT WORK, identified before a builder is
ever spawned, so it can be authored deliberately instead of discovered by a grinding loop.

Deliberately conservative. It reports what it can prove missing from canon and the migration set;
it never guesses. A clean report is not a promise the journey will pass review — only that it is
not starting against a substrate that cannot satisfy it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import canon


@dataclass
class ReadinessReport:
    journey: str
    ready: bool
    canon_gaps: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    missing_functions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def blocking(self) -> list[str]:
        out = [f"canon gap: {g}" for g in self.canon_gaps]
        out += [f"canon names entity `{t}` but no table for it exists in supabase/migrations"
                for t in self.missing_tables]
        out += [f"canon names `{fn}()` but no migration defines it" for fn in self.missing_functions]
        return out

    def brief(self) -> str:
        """What the architect is handed: the substrate to author, stated as work, not as a failure."""
        lines = [f"# Readiness gaps for {self.journey}", "",
                 "These were found BEFORE the build started, by resolving canon against the schema.",
                 "Author them first; the builder cannot, and discovering them through review costs a",
                 "full journey per finding.", ""]
        lines += [f"- {b}" for b in self.blocking()]
        return "\n".join(lines)


# `snake_case` table name for an ontology entity: BQSScore -> bqs_score, BuyerProfile -> buyer_profile
def table_name(entity: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", entity)
    return s.lower()


def _migration_text(repo: Path) -> str:
    d = repo / "supabase" / "migrations"
    if not d.is_dir():
        return ""
    return "\n".join(f.read_text(errors="ignore") for f in sorted(d.glob("*.sql")))


def _defined_tables(sql: str) -> set[str]:
    return {m.group(1).lower() for m in re.finditer(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?", sql, re.I)}


def _defined_functions(sql: str) -> set[str]:
    return {m.group(1).lower() for m in re.finditer(
        r"create\s+(?:or\s+replace\s+)?function\s+(?:[a-z_]+\.)?\"?([a-z_][a-z0-9_]*)\"?", sql, re.I)}


# Function-shaped references in canon: `publish_listing_v1(...)`, `compute_bqs_v1()`.
_FN_REF = re.compile(r"`([a-z_][a-z0-9_]{3,})\s*\(")


def check(repo: Path, journey: str) -> ReadinessReport:
    repo = Path(repo)
    rep = ReadinessReport(journey=journey, ready=True)
    try:
        bundle = canon.CanonResolver(repo).resolve(journey)
    except Exception as ex:
        rep.ready = False
        rep.canon_gaps = [f"canon could not be resolved: {ex}"]
        return rep

    # A "not found" gap means canon itself is incoherent for this journey — never dispatch that.
    rep.canon_gaps = [g for g in (bundle.gaps or []) if "not found" in g]

    sql = _migration_text(repo)
    if not sql:
        rep.notes.append("no migrations directory — substrate checks skipped")
        rep.ready = not rep.canon_gaps
        return rep

    tables, functions = _defined_tables(sql), _defined_functions(sql)

    try:
        entities = canon.CanonResolver(repo).bindings(journey) or set()
    except Exception:
        entities = set()
    for e in sorted(entities):
        if e in ("*",):
            continue
        # An ontology entity may land under a prefixed table (User -> app_user). Accept any of the
        # names the schema plausibly uses; only flag an entity that exists under none of them.
        base = table_name(e)
        if not ({base, f"app_{base}"} & tables):
            rep.missing_tables.append(e)

    text = bundle.markdown()
    for m in _FN_REF.finditer(text):
        fn = m.group(1).lower()
        # Only flag things that look like OUR database entry points, not prose or app helpers.
        if not fn.endswith(("_v1", "_v2", "_fn")):
            continue
        if fn not in functions:
            rep.missing_functions.append(fn)
    rep.missing_functions = sorted(set(rep.missing_functions))

    rep.ready = not (rep.canon_gaps or rep.missing_tables or rep.missing_functions)
    return rep
