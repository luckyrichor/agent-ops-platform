#!/usr/bin/env bash
# P9 基线矩阵驱动：按档位顺序跑三次，单独失败不停后面的档位（失败照实留证）。
set -uo pipefail
cd "$(dirname "$0")/.."

KIND="${1:-business}"
EXP="${2:-p9-baseline-20261101}"
LOG=".local/p9-baseline-${KIND}-${EXP}.log"
export AGENT_OPS_BENCH_PLANNER_CONCURRENCY="${P9_PLANNER_CONCURRENCY:-64}"

cleanup() {
  pkill -P $$ 2>/dev/null
  exit 143
}
trap cleanup TERM INT

for connections in 100 500 1000; do
  for run in 1 2 3; do
    echo "=== ${KIND} ${connections} run ${run} $(date -u +%FT%TZ)" >> "$LOG"
    uv run python scripts/p9-run-tier.py \
      --connections "$connections" --kind "$KIND" --run-index "$run" \
      --generator mac --experiment-id "$EXP" >> "$LOG" 2>&1
    echo "exit=$? $(date -u +%FT%TZ)" >> "$LOG"
  done
done
echo "matrix done" >> "$LOG"
