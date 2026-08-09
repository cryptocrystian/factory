"""Binding resolution: journey id -> the context bundle a planner/builder needs.

This automates P0 phase 0 (assembling context.md by hand). Arxus canon is prose, not yet a
structured canon.lock.yaml (Rev4), so resolution is best-effort markdown extraction driven by
the journey's own "Touches" line (the entities, events, invariants it declares) plus DEC
references found in the journey and its acceptance criteria. Output: a markdown bundle + a
structured dict. If a journey names a decision/entity that cannot be found, it is reported as a
gap rather than silently dropped (I7/I10)."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Bundle:
    journey_id: str
    sections: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        parts = [f"# Context bundle — {self.journey_id} (resolved from canon)\n"]
        for title, body in self.sections.items():
            parts.append(f"## {title}\n\n{body.strip()}\n")
        if self.gaps:
            parts.append("## Canon gaps (unresolved)\n\n" + "\n".join(f"- {g}" for g in self.gaps))
        return "\n".join(parts)


class CanonResolver:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self.canon = self.repo / "canon"
        self._journeys = self._read("Canonical Journeys v2.md")
        self._ac = self._read("Acceptance Criteria v2.md")
        self._ont = self._read("Canonical Ontology v2.md")
        self._dec = self._read("Decision Log.md")
        self._migration = (self.repo / "supabase" / "migrations" / "0001_init.sql")

    def _read(self, name: str) -> str:
        p = self.canon / name
        return p.read_text() if p.exists() else ""

    # ---- block extractors ----------------------------------------------------
    def _block(self, text: str, start_pat: str, stop_pats=(r"\n### ", r"\n## ", r"\n---")) -> str:
        m = re.search(start_pat, text)
        if not m:
            return ""
        start = m.start()
        rest = text[m.end():]
        ends = [e.start() for p in stop_pats if (e := re.search(p, rest))]
        end = min(ends) if ends else len(rest)
        return text[start:m.end() + end].strip()

    def journey(self, jid: str) -> str:
        return self._block(self._journeys, rf"### {re.escape(jid)} ")

    def acceptance(self, jid: str) -> str:
        # AC groups journeys under "## JRN-S1 — <name>"
        return self._block(self._ac, rf"## {re.escape(jid)} ", stop_pats=(r"\n## ",))

    def entity(self, name: str) -> str:
        b = self._block(self._ont, rf"\*\*\d+\. {re.escape(name)}\*\*", stop_pats=(r"\n\*\*\d+\. ", r"\n## "))
        return b

    def invariant(self, inv: str) -> str:
        m = re.search(rf"\*\*{re.escape(inv)}\*\*[^\n|]*\|?([^\n]*)", self._ont) or \
            re.search(rf"\*\*{re.escape(inv)}\*\*([^\n]*)", self._dec)
        return f"{inv}: {m.group(1).strip()}" if m else ""

    def decision(self, dec: str) -> str:
        return self._block(self._dec, rf"\*\*{re.escape(dec)} ", stop_pats=(r"\n\*\*DEC-", r"\n## "))

    def ddl(self, table: str) -> str:
        if not self._migration.exists():
            return ""
        txt = self._migration.read_text()
        m = re.search(rf"create table {re.escape(table)} \(.*?\);", txt, re.S | re.I)
        return m.group(0) if m else ""

    # ---- resolution ----------------------------------------------------------
    def resolve(self, jid: str) -> Bundle:
        b = Bundle(journey_id=jid)
        jblock = self.journey(jid)
        if not jblock:
            b.gaps.append(f"journey {jid} not found in Canonical Journeys v2")
            return b
        b.sections["Journey"] = jblock
        acblock = self.acceptance(jid)
        b.sections["Acceptance criteria"] = acblock or "(none found)"
        if not acblock:
            b.gaps.append(f"no acceptance criteria block for {jid}")

        scope = jblock + "\n" + acblock
        # entities: capitalized ontology names in the Touches line (best-effort)
        touches = re.search(r"\*\*Touches:\*\*(.+)", jblock)
        ent_names, ddl_tables = [], []
        if touches:
            for name in re.findall(r"`?([A-Z][A-Za-z]+)`?", touches.group(1)):
                if name not in ent_names and self.entity(name):
                    ent_names.append(name)
        ents = []
        for name in ent_names:
            ents.append(self.entity(name))
            table = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()   # Valuation->valuation, ListingVersion->listing_version
            if self.ddl(table):
                ddl_tables.append(table)
        if ents:
            b.sections["Bound entities"] = "\n\n".join(ents)

        # invariants + hardstops referenced
        invs = sorted(set(re.findall(r"\b(INV-\d+|HARD-\d+)\b", scope)))
        if invs:
            b.sections["Invariants & hardstops"] = "\n".join(filter(None, (self.invariant(i) or f"{i}: (see canon)" for i in invs)))

        # schema DDL for the entity tables
        ddls = [self.ddl(t) for t in ddl_tables]
        if ddls:
            b.sections["Schema (read-only; do not alter)"] = "\n\n".join(f"```sql\n{d}\n```" for d in ddls)

        # decisions referenced in journey + AC
        decs = sorted(set(re.findall(r"\bDEC-\d+\b", scope)))
        dec_bodies = [self.decision(d) for d in decs]
        dec_bodies = [d for d in dec_bodies if d]
        if dec_bodies:
            b.sections["Governing decisions"] = "\n\n".join(dec_bodies)
        for d in decs:
            if not self.decision(d):
                b.gaps.append(f"decision {d} referenced but not found")

        # L0 design canon pointer (always)
        b.sections["L0 design canon"] = (
            "Tokens: `canon/arxus-design-tokens.js` (+ `canon/Design Tokens v2 (canonical).md`). "
            "No raw hex, no rgb()/hsl(), no pure white/black (DEC-042/HARD-016). Comps/output are "
            "source-tagged; disclosures never muted (DEC-041)."
        )
        return b


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["resolve"])
    ap.add_argument("journey")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    bundle = CanonResolver(Path(a.repo)).resolve(a.journey)
    md = bundle.markdown()
    if a.out:
        Path(a.out).write_text(md)
        print(f"wrote {a.out} ({len(md)} bytes); sections={list(bundle.sections)}; gaps={bundle.gaps}")
    else:
        print(md)


if __name__ == "__main__":
    main(sys.argv[1:])
