#!/usr/bin/env bash
# Reap what the factory leaves behind on a shared box.
#
# Agents validate migrations by starting their own Postgres containers via bash. Our own gate
# script (control-plane/validate_migrations.sh) cleans up on a trap, but an agent-authored one has
# no such discipline — on 2026-08-24 five stray postgres:17 containers were found running for up to
# three days, holding ~270MB and adding to containerd's management churn. On a 2-vCPU box shared
# with production that is not housekeeping, it is a slow leak toward the fair-use cap.
#
# Conservative by construction: only containers with NO compose project (production is always
# compose-managed), only the validation image, only when older than the threshold, and never
# anything currently attached to a running lane.
set -uo pipefail

IMAGE="${JANITOR_IMAGE:-postgres:17}"
MIN_AGE_MIN="${JANITOR_MIN_AGE_MIN:-90}"
WORKTREE_KEEP="${JANITOR_WORKTREE_KEEP:-6}"
ARXUS="${ARXUS_REPO:-/root/projects/arxus}"

now=$(date +%s)
reaped=0

# 1. orphaned validation databases
while read -r id name; do
  [ -z "$id" ] && continue
  proj=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null)
  [ -n "$proj" ] && continue                      # compose-managed = production, never touch
  started=$(docker inspect -f '{{.State.StartedAt}}' "$id" 2>/dev/null)
  ts=$(date -d "$started" +%s 2>/dev/null) || continue
  age_min=$(( (now - ts) / 60 ))
  [ "$age_min" -lt "$MIN_AGE_MIN" ] && continue   # may belong to a live run
  docker rm -f "$id" >/dev/null 2>&1 && { echo "reaped container $name (${age_min}m old)"; reaped=$((reaped+1)); }
done < <(docker ps --filter "ancestor=$IMAGE" --format '{{.ID}} {{.Names}}' 2>/dev/null)

# 2. worktrees from finished runs — the branch is kept, only the checkout goes
if [ -d "$ARXUS" ]; then
  mapfile -t wts < <(git -C "$ARXUS" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}' | grep "_worktrees" || true)
  total=${#wts[@]}
  if [ "$total" -gt "$WORKTREE_KEEP" ]; then
    for w in "${wts[@]:0:$((total - WORKTREE_KEEP))}"; do
      lock="$w/.factory-inuse"
      [ -e "$lock" ] && continue
      git -C "$ARXUS" worktree remove --force "$w" >/dev/null 2>&1 && { echo "reaped worktree $(basename "$w")"; reaped=$((reaped+1)); }
    done
    git -C "$ARXUS" worktree prune >/dev/null 2>&1 || true
  fi
fi

# 3. ORPHANED worktree directories — the case step 2 is structurally blind to.
# Step 2 enumerates via `git worktree list`, which only reports REGISTERED worktrees. A run killed
# mid-flight (daemon restart, deploy) leaves its directory behind with git's registration already
# gone or never cleaned, so the most common leak is the one the janitor could not see: 5 directories
# on disk against 2 registered, the oldest sitting two weeks. A directory here is only safe to
# remove when git does not know it, nothing holds the in-use lock, and it is older than the
# container grace period — otherwise a live run could be reaped out from under itself.
WT_DIR="${WORKTREES_DIR:-/root/factory/runs/_worktrees}"
if [ -d "$WT_DIR" ] && [ -d "$ARXUS" ]; then
  registered=$(git -C "$ARXUS" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}' || true)
  for d in "$WT_DIR"/*; do
    [ -d "$d" ] || continue
    case "$registered" in *"$d"*) continue ;; esac   # git still owns it — leave it to step 2
    [ -e "$d/.factory-inuse" ] && continue
    age_min=$(( (now - $(stat -c %Y "$d" 2>/dev/null || echo "$now")) / 60 ))
    [ "$age_min" -lt "$MIN_AGE_MIN" ] && continue
    rm -rf "$d" && { echo "reaped orphaned worktree dir $(basename "$d") (${age_min}m old)"; reaped=$((reaped+1)); }
  done
  git -C "$ARXUS" worktree prune >/dev/null 2>&1 || true
fi

# 4. session transcripts — compress, never delete.
# 1443 raw .jsonl transcripts, up to 53MB per run, 1.5G total and growing ~14MB per run. These are
# the evidence trail: what each agent actually did, and the source the golden replay reads. They
# must not be deleted. But ReplayRunner._recorded() already reads `.gz` transparently (the golden
# set ships compressed, 10.5MB -> 1.5MB), so compressing costs nothing and returns ~7x. Only run
# dirs older than the grace period, so a live run is never touched mid-write.
RUNS_DIR="${RUNS_DIR:-/root/factory/runs}"
COMPRESS_AFTER_MIN="${JANITOR_COMPRESS_AFTER_MIN:-4320}"     # 3 days
if [ -d "$RUNS_DIR" ]; then
  gz=0
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    age_min=$(( (now - $(stat -c %Y "$f" 2>/dev/null || echo "$now")) / 60 ))
    [ "$age_min" -lt "$COMPRESS_AFTER_MIN" ] && continue
    gzip -q "$f" 2>/dev/null && gz=$((gz+1))
  done < <(find "$RUNS_DIR" -name "*.jsonl" -type f 2>/dev/null)
  [ "$gz" -gt 0 ] && { echo "compressed $gz transcript(s)"; reaped=$((reaped+gz)); }
fi

echo "janitor: reaped $reaped item(s)"
