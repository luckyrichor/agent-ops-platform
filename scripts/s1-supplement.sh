#!/usr/bin/env bash
# S1 补充实验：留一法、复合索引、W3 队首抢占。全部 W2 饱和负载（思考 0）。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
BENCH="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20"
IDX="uv run python scripts/s1-index.py"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-supplement.log
  $BENCH --label "$label" --out "$OUT/${label}.json" "$@" >> .local/s1-supplement.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-supplement.log
}

# 1) 留一法：完整 B / 去索引 / 去游标 / 去 SKIP LOCKED，与 B 同轮配对
for round in 1 2 3; do
  $IDX create --kind partial > "$OUT/idx-loo-${round}.json"
  run "loo-B-${round}" --group B --claim-mode skip_locked --page-mode keyset --pick head --refill
  $IDX drop --kind partial > "$OUT/idx-loo-noindex-${round}.json"
  run "loo-noindex-${round}" --group B --claim-mode skip_locked --page-mode keyset --pick head --refill
  $IDX create --kind partial > /dev/null
  run "loo-nocursor-${round}" --group B --claim-mode skip_locked --page-mode offset --pick head --refill
  run "loo-noskip-${round}" --group C --claim-mode cas --page-mode keyset --pick head --refill
done

# 2) 普通复合索引 vs 部分索引（配置与完整 B 相同，只换索引类型）
$IDX drop --kind partial > /dev/null
$IDX create --kind composite > "$OUT/idx-composite-create.json"
for round in 1 2 3; do
  run "comp-${round}" --group X --claim-mode skip_locked --page-mode keyset --pick head --refill
done
$IDX stats --kind composite > "$OUT/idx-composite-stats.json"
$IDX drop --kind composite > /dev/null

# 3) W3 队首抢占：A 基线 / 索引+游标+CAS / 完整 B，各 5 次同轮配对
for round in 1 2 3 4 5; do
  $IDX drop --kind partial > /dev/null
  run "w3-a-${round}" --group A --claim-mode cas --page-mode offset --pick head --refill
  $IDX create --kind partial > /dev/null
  run "w3-k-${round}" --group K --claim-mode cas --page-mode keyset --pick head --refill
  run "w3-b-${round}" --group B --claim-mode skip_locked --page-mode keyset --pick head --refill
done

$IDX stats --kind partial > "$OUT/idx-final.json"
echo "supplement done $(date -u +%FT%TZ)" >> .local/s1-supplement.log
