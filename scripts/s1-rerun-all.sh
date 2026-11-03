#!/usr/bin/env bash
# S1 复核重跑：留一法、W3、复合 vs 部分，全部用最终脚本与环境指纹。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
IDX="uv run python scripts/s1-index.py"
BASE="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20 --refill --expect-waiting 50000 --expect-total 1000000"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-rerun-all.log
  "$@" --label "$label" --out "$OUT/${label}.json" >> .local/s1-rerun-all.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-rerun-all.log
}

# 留一法
for round in 1 2 3; do
  $IDX create --kind partial > /dev/null
  run "v2-loo-B-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX drop --kind partial > /dev/null
  run "v2-loo-noindex-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "v2-loo-nocursor-${round}" $BASE --group B --claim-mode skip_locked --page-mode offset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  run "v2-loo-noskip-${round}" $BASE --group C --claim-mode cas --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
done

# W3 队首抢占
for round in 1 2 3 4 5; do
  $IDX drop --kind partial > /dev/null
  run "v2-w3-a-${round}" $BASE --group A --claim-mode cas --page-mode offset --pick head \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "v2-w3-k-${round}" $BASE --group K --claim-mode cas --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  run "v2-w3-b-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
done

# 复合 vs 部分
for round in 1 2 3 4 5; do
  $IDX create --kind partial > /dev/null
  run "v2-pc-${round}-partial" $BASE --group P --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX drop --kind partial > /dev/null
  $IDX create --kind composite > /dev/null
  run "v2-pc-${round}-composite" $BASE --group X --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_composite --forbid-index s1_queue_partial
  $IDX drop --kind composite > /dev/null
done
$IDX create --kind partial > /dev/null

echo "rerun-all done $(date -u +%FT%TZ)" >> .local/s1-rerun-all.log
