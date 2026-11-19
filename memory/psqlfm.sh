#!/usr/bin/env bash
# psqlfm.sh - psql wrapper for fleet_memory. Password read from agent/.env at runtime; never stored here.
set -euo pipefail
DU=$(grep -h "^[[:space:]]*DATABASE_URL=" /home/lumen/agent/.env | head -1 | sed -E "s/^[[:space:]]*DATABASE_URL=//")
PW=$(printf "%s" "$DU" | sed -E "s#.*://[^:]+:([^@]+)@.*#\1#")
export PGPASSWORD="$PW"
exec psql -h 127.0.0.1 -p 5433 -U lumen -d "${FMDB:-fleet_memory}" "$@"
