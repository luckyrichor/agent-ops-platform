#!/usr/bin/env bash
# S1 最终复核（v3）：每轮前 VACUUM FULL，A/B、留一法、W3、复合对比统一口径。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
IDX="uv run python scripts/s1-index.py"
BASE="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20 --refill --expect-waiting 50000 --expect-total 1000000"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-v3.log
  "$@" --label "$label" --out "$OUT/${label}.json" >> .local/s1-v3.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-v3.log
}

# A/B
for round in 1 2 3 4 5; do
  $IDX drop --kind partial > /dev/null
  run "v3-ab-${round}-a" $BASE --group A --claim-mode cas --page-mode offset --pick random \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "v3-ab-${round}-b" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
done

# 留一法
for round in 1 2 3; do
  $IDX create --kind partial > /dev/null
  run "v3-loo-B-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial
  $IDX drop --kind partial > /dev/null
  run "v3-loo-noindex-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "v3-loo-nocursor-${round}" $BASE --group B --claim-mode skip_locked --page-mode offset --pick head \
      --require-index s1_queue_partial
  run "v3-loo-noskip-${round}" $BASE --group C --claim-mode cas --page-mode keyset --pick head \
      --require-index s1_queue_partial
done

# W3
for round in 1 2 3 4 5; do
  $IDX drop --kind partial > /dev/null
  run "v3-w3-a-${round}" $BASE --group A --claim-mode cas --page-mode offset --pick head \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "v3-w3-k-${round}" $BASE --group K --claim-mode cas --page-mode keyset --pick head \
      --require-index s1_queue_partial
  run "v3-w3-b-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial
done

# 部分 vs 复合
for round in 1 2 3 4 5; do
  $IDX create --kind partial > /dev/null
  run "v3-pc-${round}-partial" $BASE --group P --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX drop --kind partial > /dev/null
  $IDX create --kind composite > /dev/null
  run "v3-pc-${round}-composite" $BASE --group X --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_composite --forbid-index s1_queue_partial
  $IDX drop --kind composite > /dev/null
done
$IDX create --kind partial > /dev/null

echo "v3 done $(date -u +%FT%TZ)" >> .local/s1-v3.log
