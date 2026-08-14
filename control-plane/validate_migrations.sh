#!/usr/bin/env bash
# Deterministic migration gate for Supabase-stack repos: apply every migration in order to a
# throwaway Postgres and fail if any does not apply clean. This is what makes an architect-authored
# migration verified by CODE, not by the agent's own say-so (agent proposes, code disposes).
#
# Usage: validate_migrations.sh [migrations_dir]   (default: ./supabase/migrations relative to cwd)
# Exit 0 = all applied clean (or no migrations dir → nothing to validate). Nonzero = a failure,
# with the offending migration + psql error on stdout.
set -uo pipefail

MIGDIR="${1:-supabase/migrations}"
[ -d "$MIGDIR" ] || { echo "migrations:apply — no $MIGDIR, nothing to validate"; exit 0; }

command -v docker >/dev/null 2>&1 || { echo "migrations:apply — docker unavailable, cannot validate"; exit 3; }

CN="miggate-$$-${RANDOM:-0}"
cleanup() { docker rm -f "$CN" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$CN" -e POSTGRES_PASSWORD=x -e POSTGRES_DB=app postgres:17 >/dev/null 2>&1 \
  || { echo "migrations:apply — could not start postgres:17"; exit 3; }

# Real readiness (postgres restarts once during init).
ready=
for _ in $(seq 1 40); do
  docker exec "$CN" psql -U postgres -d app -tAc "select 1" >/dev/null 2>&1 && { ready=1; break; }
  sleep 1
done
[ -n "$ready" ] || { echo "migrations:apply — postgres never became ready"; exit 3; }

# Supabase-provided objects a migration may reference (auth schema/helpers, standard roles).
docker exec -i "$CN" psql -U postgres -d app -v ON_ERROR_STOP=1 -q >/dev/null 2>&1 <<'SHIM'
create extension if not exists pgcrypto; create extension if not exists citext;
do $$ begin create role anon; exception when duplicate_object then null; end $$;
do $$ begin create role authenticated; exception when duplicate_object then null; end $$;
do $$ begin create role service_role; exception when duplicate_object then null; end $$;
do $$ begin create role authenticator; exception when duplicate_object then null; end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $f$ select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $f$;
create or replace function auth.role() returns text language sql stable as $f$ select nullif(current_setting('request.jwt.claim.role', true),'') $f$;
create or replace function auth.jwt() returns jsonb language sql stable as $f$ select coalesce(nullif(current_setting('request.jwt.claims', true),''),'{}')::jsonb $f$;
SHIM

rc=0
for m in $(ls "$MIGDIR"/*.sql 2>/dev/null | sort); do
  if docker exec -i "$CN" psql -U postgres -d app -v ON_ERROR_STOP=1 -q < "$m" 2>/tmp/miggate.$$; then
    echo "  ok  $(basename "$m")"
  else
    echo "  FAIL $(basename "$m"):"; grep -iE "error|line [0-9]" /tmp/miggate.$$ | head -5
    rc=2; break
  fi
done
rm -f /tmp/miggate.$$
[ "$rc" = 0 ] && echo "migrations:apply — all $(ls "$MIGDIR"/*.sql 2>/dev/null | wc -l) migrations apply clean"
exit $rc
