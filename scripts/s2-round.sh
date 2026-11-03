#!/usr/bin/env bash
# S2 单轮：模板库克隆 + 受限 slice 双实例 + 200 并发负载 + cgroup 记录。
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .local/s1/db.env
set +a

ROUND="s2r$(date +%s)"
PW="$S1_PG_PASSWORD"
BASE_URL="${S1_DATABASE_URL%/*}"
CACHE="${1:-0}"
OUT="docs/measurements/2026-11-03-s2/${ROUND}.json"

docker exec -e PGPASSWORD="$PW" s1-postgres psql -U ops_owner -d postgres -q -c \
  "CREATE DATABASE $ROUND TEMPLATE s2_template" || exit 1

ROUND_URL="$BASE_URL/$ROUND"
export S1_DATABASE_URL="$ROUND_URL"
export PYTHONPATH="$PWD/src"

start() {
  local port="$1" unit="$2"
  systemd-run --user --unit="$unit" --collect \
    -p CPUQuota=100% -p MemoryMax=512M -p MemorySwapMax=0 \
    --working-directory="$PWD" \
    --setenv=S1_DATABASE_URL="$ROUND_URL" --setenv=PYTHONPATH="$PWD/src" \
    --setenv=S2_CACHE="$CACHE" \
    "$PWD/.venv/bin/python" -m uvicorn agent_ops.bench.s2_app:app \
      --host 127.0.0.1 --port "$port" --log-level warning >/dev/null 2>&1
}

A="s2a-$ROUND"; B="s2b-$ROUND"
start 18911 "$A"; start 18912 "$B"
sleep 6
S2_TAG_CACHE="$CACHE" uv run python scripts/s2-load.py --out "$OUT" >/dev/null 2>&1
STATUS=0
for unit in "$A" "$B"; do
  systemctl --user show "$unit" -p MemoryPeak -p CPUUsageNSec >> /tmp/s2-cgroup.txt 2>/dev/null
  systemctl --user stop "$unit" >/dev/null 2>&1
done
cp /tmp/s2-cgroup.txt "${OUT%.json}-cgroup.txt" 2>/dev/null
docker exec -e PGPASSWORD="$PW" s1-postgres psql -U ops_owner -d postgres -q -c \
  "DROP DATABASE $ROUND WITH (FORCE)" >/dev/null 2>&1
echo "$OUT"
exit $STATUS
