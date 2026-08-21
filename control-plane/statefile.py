"""Locked, atomic read-modify-write for the files two processes share.

`backlog.yml` and `decisions.yml` are written by BOTH the daemon and the observatory, with no
coordination: the daemon read the whole backlog at the top of a 20-second cycle and wrote the whole
thing back at the end, so a ruling approved in the UI during that window was silently erased and the
item stayed escalated forever. The approvals UI is only trustworthy if a concurrent writer cannot
lose it.

Two rules, both enforced here rather than remembered at each call site:
  1. Read-modify-write happens under an exclusive lock, so the window closes.
  2. The mutation is applied to what is ON DISK NOW, never to a stale in-memory copy.
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path

import yaml


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def read(path: Path, default=None):
    """Read under a shared lock. Never blocks a writer for longer than the read itself."""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)
        try:
            return yaml.safe_load(path.read_text()) or (default if default is not None else {})
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def update(path: Path, mutate, header: str = "", default=None):
    """Apply `mutate(data)` to the CURRENT on-disk contents under an exclusive lock, then write it
    atomically. `mutate` may return the new data or mutate in place. Returns the written data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = (yaml.safe_load(path.read_text()) if path.exists() else None)
            if data is None:
                data = default if default is not None else {}
            out = mutate(data)
            if out is None:
                out = data
            body = (header or "") + yaml.safe_dump(out, sort_keys=False, default_flow_style=False)
            # Atomic replace: a reader never sees a half-written queue.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(body)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return out
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
