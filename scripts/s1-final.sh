#!/usr/bin/env bash
# S1 最终实验：同轮 A/B、部分 vs 复合索引配对、写入成本、补给重复复核。
# 所有运行都带环境指纹断言（索引清单、待人工数、总行数）。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
IDX="uv run python scripts/s1-index.py"
BASE="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20 --refill --expect-waiting 50000 --expect-total 1000000"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-final.log
  "$@" --label "$label" --out "$OUT/${label}.json" >> .local/s1-final.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-final.log
}

# A) 最终 A/B 配对（唯一吞吐结论来源）
for round in 1 2 3 4 5; do
  $IDX drop --kind partial > /dev/null
  run "final-ab-${round}-a" $BASE --group A --claim-mode cas --page-mode offset --pick random \
      --forbid-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX create --kind partial > /dev/null
  run "final-ab-${round}-b" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
done

# B) 部分索引 vs 复合索引同轮配对
for round in 1 2 3 4 5; do
  $IDX create --kind partial > /dev/null
  run "final-pc-${round}-partial" $BASE --group P --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial --forbid-index s1_queue_composite
  $IDX drop --kind partial > /dev/null
  $IDX create --kind composite > /dev/null
  run "final-pc-${round}-composite" $BASE --group X --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_composite --forbid-index s1_queue_partial
  $IDX drop --kind composite > /dev/null
done
$IDX create --kind partial > /dev/null

# C) 写入成本：无索引 / 部分索引 / 复合索引，各 3 轮插入与更新
write_config() {
  local name="$1"; local require="$2"; local forbid="$3"
  for round in 1 2 3; do
    run "final-write-${name}-insert-${round}" uv run python scripts/s1-bench.py --mode write \
        --write-op insert --write-rows 20000 --expect-waiting 50000 --expect-total 1000000 \
        --require-index "$require" $( [ -n "$forbid" ] && echo "--forbid-index $forbid" )
    run "final-write-${name}-update-${round}" uv run python scripts/s1-bench.py --mode write \
        --write-op update --write-rows 20000 --expect-waiting 50000 --expect-total 1000000 \
        --require-index "$require" $( [ -n "$forbid" ] && echo "--forbid-index $forbid" )
  done
}

$IDX drop --kind partial > /dev/null
write_config none tickets_pkey s1_queue_partial
$IDX create --kind partial > /dev/null
write_config partial s1_queue_partial s1_queue_composite
$IDX drop --kind partial > /dev/null
$IDX create --kind composite > /dev/null
write_config composite s1_queue_composite s1_queue_partial
$IDX drop --kind composite > /dev/null
$IDX create --kind partial > /dev/null

# D) 补给轮重复数复核（完整 B）
for round in 1 2 3; do
  run "final-dup-b-${round}" $BASE --group B --claim-mode skip_locked --page-mode keyset --pick head \
      --require-index s1_queue_partial
done

echo "final matrix done $(date -u +%FT%TZ)" >> .local/s1-final.log
