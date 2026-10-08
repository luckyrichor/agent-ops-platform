#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# The existing workspace database image provides a shell for one-off volume ownership.
# This starts no PostgreSQL process and touches only the dedicated tracing volume root.
docker volume create agent-ops-tracing-data >/dev/null
docker run --rm --user 0 --entrypoint /bin/sh \
  -v agent-ops-tracing-data:/data pgvector/pgvector:pg16 \
  -c 'chown 10001:10001 /data'
docker compose -f compose.tracing.yaml up -d
