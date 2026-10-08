#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# A minimal, digest-pinned shell initializes only the dedicated tracing volume root.
docker volume create agent-ops-tracing-data >/dev/null
docker run --rm --user 0 --entrypoint /bin/sh \
  -v agent-ops-tracing-data:/data busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e \
  -c 'chown 10001:10001 /data'
docker compose -f compose.tracing.yaml up -d
