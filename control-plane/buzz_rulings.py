#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Ingest owner rulings typed in Buzz, and apply them to the backlog.

The escalation half of the decision plane has worked for a while: the factory posts what it cannot
decide. The return half did not exist — a ruling had to come back through a human relaying it into
YAML, which put a person in the middle of the loop the factory was built to keep them out of.

Reply syntax, printed in every escalation so it is never something to remember:

    RULE <item-id>: approve <the ruling>
    RULE <item-id>: reject <why>

An approval re-opens the item (status -> ready) so the daemon picks it back up; a rejection leaves
it escalated and records why. Rulings are recorded in the same decisions queue the observatory
serves, so Buzz and the dashboard are two doors onto one ledger, never two sources of truth.

    uv run control-plane/buzz_rulings.py [--once] [--since-hours N]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))
FACTORY_ROOT = CP.parent

import statefile  # noqa: E402

BACKLOG = FACTORY_ROOT / "backlog.yml"
DECISIONS = FACTORY_ROOT / "runs" / "decisions.yml"
SEEN = FACTORY_ROOT / "runs" / ".buzz-rulings-seen"
BACKLOG_HEADER = "# Factory backlog — the orchestrator's work queue. Edit status to re-open an item.\n"
DECISIONS_HEADER = "# Decisions queue — escalations + ratifications awaiting the human.\n"

RULE = re.compile(r"^\s*RULE\s+([A-Za-z0-9_.\-]+)\s*:\s*(approve|reject)\b\s*(.*)$",
                  re.IGNORECASE | re.MULTILINE)

# A ruling must come from a HUMAN, and the factory must never rule itself.
#
# Every escalation the factory posts ends with the reply syntax so the owner does not have to
# remember it — "RULE jrn-b1: approve <your ruling>". The ingester then read the channel, matched
# those template lines in the factory's OWN post, and applied them: approve, then reject. Items were
# auto-ruled, flipped back to ready, and re-dispatched, while the owner's queue showed nothing open.
# The factory was answering its own questions.
#
# Three layers, because one is not enough for something that can silently grant its own approvals:
#   1. Never act on an event we authored (identity).
#   2. Never act on a line that still carries a placeholder (it is a template, not a decision).
#   3. Never act on a line inside a message that is itself an escalation post (shape).
_PLACEHOLDER = re.compile(r"<[^>]{0,40}>")
_ESCALATION_MARKERS = ("⚑ ESCALATION", "⚑ DECISION", "To rule it, reply in this channel")


def is_template_line(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text))


def looks_like_factory_post(content: str) -> bool:
    return any(m in (content or "") for m in _ESCALATION_MARKERS)


def _seen() -> set[str]:
    try:
        return set(SEEN.read_text().split())
    except OSError:
        return set()


def _mark(event_id: str) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN, "a") as fh:
        fh.write(event_id + "\n")


def apply_ruling(item_id: str, verdict: str, text: str, author: str, event_id: str) -> str:
    """Record the ruling and, on approval, re-open the item. Both files under the shared lock, so a
    ruling cannot be lost to a concurrent daemon write."""
    approved = verdict.lower() == "approve"

    def _record(cur):
        cur.setdefault("decisions", [])
        hit = [d for d in cur["decisions"] if d.get("backlog_id") == item_id and d.get("status") == "open"]
        if not hit:                                   # a ruling with no open decision is still recorded
            cur["decisions"].append({"id": f"{item_id}-buzz-{event_id[:8]}", "backlog_id": item_id,
                                     "kind": "ruling", "finding": "(ruled in Buzz)",
                                     "status": "approved" if approved else "rejected",
                                     "human_action": verdict, "human_note": text, "via": "buzz",
                                     "author": author})
            return cur
        for d in hit:
            d["status"] = "approved" if approved else "rejected"
            d["human_action"] = verdict
            d["human_note"] = text
            d["via"] = "buzz"
            d["author"] = author
        return cur

    statefile.update(DECISIONS, _record, header=DECISIONS_HEADER, default={"decisions": []})

    if not approved:
        return f"recorded rejection for {item_id}"

    found = {"hit": False}

    def _reopen(bl):
        for it in bl.get("items", []):
            if it["id"] == item_id:
                found["hit"] = True
                it["status"] = "ready"
                it["note_reason"] = f"ruled in Buzz by {author}: {text[:200]}"
        return bl

    statefile.update(BACKLOG, _reopen, header=BACKLOG_HEADER, default={"items": []})
    return (f"{item_id} -> ready (ruled in Buzz)" if found["hit"]
            else f"ruling recorded, but no backlog item named {item_id}")


def poll(since_hours: float = 48.0, limit: int = 200) -> list[str]:
    import buzz
    import notifier as notifier_mod
    cfg = notifier_mod._load_cfg()
    client = buzz.BuzzClient.from_env()
    channels = []
    for _proj, facets in (cfg.get("channels") or {}).items():
        if isinstance(facets, dict):
            channels += [v for v in facets.values() if isinstance(v, str)]
        elif isinstance(facets, str):
            channels.append(facets)
    cutoff = time.time() - since_hours * 3600
    seen, out = _seen(), []
    try:
        # `pubkey` is a property on BuzzClient, not a method. Calling it raised
        # TypeError, the bare except swallowed it, and `me` was always empty --
        # which silently disabled layer 1 below (never act on our own event).
        me = (client.pubkey or "").lower()
    except Exception as ex:
        # Identity is a safety control, so losing it must be visible rather than
        # quietly downgrading to "trust every author".
        print(f"  ⚠ cannot determine our own pubkey ({type(ex).__name__}: {ex}); "
              f"self-authorship check is DISABLED for this pass", flush=True)
        me = ""
    for ch in dict.fromkeys(channels):
        try:
            events = client.read_channel(ch, limit=limit)
        except Exception as ex:
            out.append(f"channel {ch[:8]}: unreachable ({type(ex).__name__})")
            continue
        for ev in events or []:
            eid = ev.get("id", "")
            if not eid or eid in seen or (ev.get("created_at") or 0) < cutoff:
                continue
            author = (ev.get("pubkey") or "").lower()
            content = ev.get("content") or ""
            if me and author == me:
                _mark(eid)                       # our own post: never a ruling
                continue
            if looks_like_factory_post(content):
                _mark(eid)                       # an escalation, not a decision about one
                continue
            for m in RULE.finditer(content):
                item_id, verdict, text = m.group(1), m.group(2), m.group(3).strip()
                if is_template_line(m.group(0)):
                    out.append(f"ignored template line for {item_id} (contains a placeholder)")
                    continue
                out.append(apply_ruling(item_id, verdict, text, author[:12], eid))
            _mark(eid)
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--since-hours", type=float, default=48.0)
    ap.add_argument("--poll", type=int, default=60, help="seconds between polls when not --once")
    a = ap.parse_args(argv)
    while True:
        for line in poll(a.since_hours):
            print(f"  ⚖ {line}", flush=True)
        if a.once:
            return 0
        time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
