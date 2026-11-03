#!/usr/bin/env bash
# S1 矩阵：A/A、A/B、消融、两步演示、W1。索引开关按配置切换。
set -uo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-docs/measurements/2026-11-03-s1}"
mkdir -p "$OUT"
BENCH="uv run python scripts/s1-bench.py --agents 50 --list-clients 20 --seconds 60 --pages 1,100,1000,2500 --page-size 20"
IDX="uv run python scripts/s1-index.py"

run() {
  local label="$1"; shift
  echo "=== ${label} $(date -u +%FT%TZ)" >> .local/s1-matrix.log
  $BENCH --label "$label" --out "$OUT/${label}.json" "$@" >> .local/s1-matrix.log 2>&1
  echo "exit=$? $(date -u +%FT%TZ)" >> .local/s1-matrix.log
}

# W2：A/A 5 轮（无索引）
$IDX drop > "$OUT/index-before-aa.json"
for round in 1 2 3 4 5; do
  run "aa-${round}-a" --group A --claim-mode cas --page-mode offset --refill
  run "aa-${round}-b" --group A --claim-mode cas --page-mode offset --refill
done

# W2：A/B 5 轮（A 无索引，B 有索引）
for round in 1 2 3 4 5; do
  $IDX drop > "$OUT/index-drop-${round}.json"
  run "ab-${round}-baseline" --group A --claim-mode cas --page-mode offset --refill
  $IDX create > "$OUT/index-create-${round}.json"
  run "ab-${round}-optimized" --group B --claim-mode skip_locked --page-mode keyset --refill
done

# 消融（各 3 次）
$IDX drop > "$OUT/index-abl-keyset.json"
for round in 1 2 3; do
  run "abl-keyset-${round}" --group K --claim-mode cas --page-mode keyset --refill
done
$IDX create > "$OUT/index-abl-index.json"
for round in 1 2 3; do
  run "abl-index-${round}" --group I --claim-mode cas --page-mode offset --refill
done
$IDX drop > "$OUT/index-abl-skip.json"
for round in 1 2 3; do
  run "abl-skip-${round}" --group S --claim-mode skip_locked --page-mode offset --refill
done

# 两步领取演示（不参与提升倍数）
for round in 1 2 3; do
  run "naive-${round}" --group N --claim-mode two_step --page-mode offset --refill
done

# W1 真实节奏（思考 2 秒，辅助指标）
$IDX drop > "$OUT/index-w1-a.json"
for round in 1 2 3; do
  run "w1-a-${round}" --group A --claim-mode cas --page-mode offset --think 2
done
$IDX create > "$OUT/index-w1-b.json"
for round in 1 2 3; do
  run "w1-b-${round}" --group B --claim-mode skip_locked --page-mode keyset --think 2
done

echo "matrix done $(date -u +%FT%TZ)" >> .local/s1-matrix.log
