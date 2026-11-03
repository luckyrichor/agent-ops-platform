#!/usr/bin/env bash
# 修正 keyset 游标后重跑 B 侧：optimized ×5、only-keyset ×3、W1-B ×3。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
BENCH="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20"
IDX="uv run python scripts/s1-index.py"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-rerun-b.log
  $BENCH --label "$label" --out "$OUT/${label}.json" "$@" >> .local/s1-rerun-b.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-rerun-b.log
}

$IDX create > "$OUT/index-rerun-b.json"
for round in 1 2 3 4 5; do
  run "ab-${round}-optimized" --group B --claim-mode skip_locked --page-mode keyset --refill
done
for round in 1 2 3; do
  run "abl-keyset-${round}" --group K --claim-mode cas --page-mode keyset --refill
done
for round in 1 2 3; do
  run "w1-b-${round}" --group B --claim-mode skip_locked --page-mode keyset --think 2
done

echo "rerun done $(date -u +%FT%TZ)" >> .local/s1-rerun-b.log
