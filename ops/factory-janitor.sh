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

echo "janitor: reaped $reaped item(s)"
