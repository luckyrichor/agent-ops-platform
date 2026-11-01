#!/usr/bin/env bash
# P9 bench 隔离库：独立数据库 + 真实迁移 + 只读复制 P2 合成目录。
# 不复制订单/工单/支付/账号数据；不修改现有 agent_ops 库。
# 属主负责建库与读取源数据，运行角色只在 bench 库内工作。
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .local/application.env
# shellcheck disable=SC1091
source .local/stack.env
set +a

CONTAINER="${P9_PG_CONTAINER:-agent-ops-tracing-postgres-1}"
SOURCE_DB="${P9_SOURCE_DB:-agent_ops_p2_seed42}"
BENCH_DB="${P9_BENCH_DB:-agent_ops_bench}"

python3 - "$AGENT_OPS_DATABASE_URL" "$BENCH_DB" <<'PY' > /tmp/p9-db-parts.sh
import sys
from urllib.parse import unquote, urlsplit
url = urlsplit(sys.argv[1])
print(f"export RUNTIME_USER={url.username}")
print(f"export RUNTIME_PASSWORD={unquote(url.password or '')}")
print(f"export PGUSER={url.username}")
print(f"export PGPASSWORD={unquote(url.password or '')}")
print(f"export PGHOST=127.0.0.1")
print(f"export PGPORT={url.port}")
print(f"export BENCH_URL=\"{sys.argv[1].rsplit('/',1)[0]}/{sys.argv[2]}\"")
PY
# shellcheck disable=SC1091
source /tmp/p9-db-parts.sh
rm -f /tmp/p9-db-parts.sh

owner() {
  docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
    psql -U ops_owner -d "${1:-postgres}" -v ON_ERROR_STOP=1 "${@:2}"
}

exists=$(owner postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$BENCH_DB'")
if [ "$exists" != "1" ]; then
  owner postgres -c "CREATE DATABASE $BENCH_DB OWNER $RUNTIME_USER"
fi

AGENT_OPS_DATABASE_URL="$BENCH_URL" uv run alembic upgrade head >/dev/null

count=$(docker exec -i -e PGPASSWORD="$RUNTIME_PASSWORD" "$CONTAINER" \
  psql -U "$RUNTIME_USER" -d "$BENCH_DB" -tAc "SELECT count(*) FROM commerce.products")
if [ "$count" = "0" ]; then
  docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
    pg_dump -U ops_owner -d "$SOURCE_DB" --data-only \
    -t commerce.categories -t commerce.products -t commerce.inventory \
    | docker exec -i -e PGPASSWORD="$RUNTIME_PASSWORD" "$CONTAINER" \
      psql -U "$RUNTIME_USER" -d "$BENCH_DB" -q -v ON_ERROR_STOP=1
fi

docker exec -i -e PGPASSWORD="$RUNTIME_PASSWORD" "$CONTAINER" \
  psql -U "$RUNTIME_USER" -d "$BENCH_DB" -tAc \
  "SELECT (SELECT count(*) FROM commerce.products) || '/' || (SELECT count(*) FROM commerce.inventory)"
