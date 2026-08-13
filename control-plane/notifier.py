"""Notifier port — the factory's swappable collaboration/notification seam.

The orchestrator (and lanes) emit human-facing lifecycle events — escalations that
need a ruling, acceptances worth surfacing — through this port instead of hard-wiring
a chat app. Default adapter is Buzz (control-plane/buzz.py); `adapter: null` degrades
to file-only. Like the tracer, a notifier failure can NEVER break a run: every call
is best-effort and swallows its own errors.

Config: factory/notify.yml
  adapter: buzz | null
  channels: {default: <uuid>, <project>: <uuid>, ...}   # project == backlog `repo`
  events:   {escalated: true, accepted: true}
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

CP = Path(__file__).resolve().parent
FACTORY_ROOT = CP.parent
NOTIFY_CFG = FACTORY_ROOT / "notify.yml"
OBSERVE_URL = "http://localhost:7788"


def _load_cfg() -> dict:
    if NOTIFY_CFG.exists():
        try:
            return yaml.safe_load(NOTIFY_CFG.read_text()) or {}
        except Exception:
            return {}
    return {}


class Notifier:
    """Port interface. Every method is best-effort and returns the adapter result
    (or None). Callers never depend on the return value or on success."""
    enabled = False

    def escalation(self, *, project, item_id, kind, note, run_id, blocking, url=None):
        return None

    def accepted(self, *, project, item_id, kind, note, run_id):
        return None

    def post(self, project=None, text=""):
        return None


class NullNotifier(Notifier):
    """File-only mode: the orchestrator still writes runs/escalations/*.md; this
    just posts nothing externally."""
    enabled = False


class BuzzNotifier(Notifier):
    enabled = True

    def __init__(self, client, channels, events):
        self.client = client
        self.channels = channels or {}
        self.events = events if events is not None else {}

    def _channel(self, project, facet="home"):
        """Resolve (project/initiative, facet/track) -> channel UUID. Supports both the nested
        per-initiative form {init: {home: uuid, product: uuid, ...}} and the legacy flat form
        {init: uuid}. Falls back facet -> home -> product -> default."""
        ch = self.channels.get(project)
        if isinstance(ch, dict):
            return (ch.get(facet) or ch.get("home") or ch.get("product")
                    or self.channels.get("default"))
        if isinstance(ch, str):
            return ch
        return self.channels.get("default")

    def post(self, project=None, text="", facet="home"):
        ch = self._channel(project, facet)
        if not ch:
            print(f"  (notifier: no channel for project={project!r} facet={facet!r}, skipping)", file=sys.stderr)
            return None
        try:
            return self.client.send_message(ch, text)
        except Exception as e:  # never break the run
            print(f"  (notifier: post to {project} failed: {e})", file=sys.stderr)
            return None

    def escalation(self, *, project, item_id, kind, note, run_id, blocking, url=None):
        if not self.events.get("escalated", True):
            return None
        lines = [f"⚑ ESCALATION — {item_id}",
                 f"project: {project} · kind: {kind}"]
        if note:
            lines.append(note)
        lines += ["",
                  "The factory ran this and would not fake a green. Blocking findings from the "
                  "independent reviewer:"]
        lines += [f"  • {b}" for b in (blocking or ["(no structured findings captured — see the run)"])]
        lines += ["",
                  "Your ruling is needed (usually a DEC in canon, then set the item ready and re-run).",
                  f"Run: {run_id}",
                  f"Observatory: {url or OBSERVE_URL}"]
        return self.post(project, "\n".join(lines), facet="product")

    def accepted(self, *, project, item_id, kind, note, run_id):
        if not self.events.get("accepted", True):
            return None
        lines = [f"✓ accepted — {item_id} ({kind}) on {project}"]
        if note:
            lines.append(note)
        lines.append(f"Run: {run_id}")
        return self.post(project, "\n".join(lines), facet="product")


def get_notifier() -> Notifier:
    """Resolve the configured notifier. Any failure (unconfigured, creds missing,
    relay unreachable) degrades to NullNotifier — the factory keeps working."""
    cfg = _load_cfg()
    adapter = (cfg.get("adapter") or "buzz").lower()
    if adapter == "null":
        return NullNotifier()
    try:
        if str(CP) not in sys.path:
            sys.path.insert(0, str(CP))
        from buzz import BuzzClient
        client = BuzzClient.from_env()
        return BuzzNotifier(client, cfg.get("channels"), cfg.get("events"))
    except Exception as e:
        print(f"  (notifier: Buzz unavailable, degrading to file-only: {e})", file=sys.stderr)
        return NullNotifier()
