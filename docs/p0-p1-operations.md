# P0/P1 本地环境与数据层操作

源码、环境版本与实测结果见 [交付报告](reports/p0-p1-2026-10-21/delivery.md)。本页只给操作入口。

## 启动

```bash
uv sync --all-groups
bash scripts/start-stack.sh
uv run python scripts/serve.py --backend postgres
```

栈默认只运行在tx。应用API在127.0.0.1:8003，由前台命令显式运行；没有部署公网或常驻业务服务。新Collector入口14318，保留原Jaeger直达4318。

| 组件 | 主机本地端口 |
|---|---:|
| PostgreSQL | 55433 |
| Redis | 16380 |
| Collector OTLP HTTP | 14318 |
| Collector健康 | 13134 |
| Prometheus | 19091 |
| Grafana | 13001 |
| Jaeger UI | 16686 |

生成的.local/stack.env和.local/application.env只在服务器保存，目录700、文件600，不打印或发群。Grafana用户名admin，口令在本地私密配置里。应用使用ops_runtime角色，不使用迁移owner。迁移由启动脚本以owner执行；不要让API自行运行迁移。

脚本可重复运行，保持原数据卷。compose.yaml复用agent-ops-tracing项目与已有外部trace卷；不要使用down -v删除数据。原compose.tracing.yaml是历史追踪独立入口，现在统一使用start-stack.sh，避免独立管理同项目生命周期。

## 内存基线开关

```bash
uv run python scripts/serve.py --backend memory --port 8004
```

原SupportService与容量/幂等行为保持。create_app工厂默认memory；正常PG模式用显式serve入口。PG连接失败不会悄悄退回内存。baseline-v0与baseline-b0标签，以及docs/baselines/b0目录，均保留。

## 迁移与验证

Alembic使用AGENT_OPS_DATABASE_URL；隔离库可upgrade head、downgrade base再upgrade head。downgrade base删除整个support schema及P1业务数据，已经在临时测试库验证，不用它操作持久库日常回滚。生产备份/还原演练属于P12，尚未完成。

```bash
AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q
uv run ruff check src tests migrations
uv run mypy src
uv run python -m agent_ops.evals.support_runner
uv run python scripts/verify-stack.py --output .local/stack-connectivity.json
uv run python scripts/verify-p1-http.py --output .local/p1-http.json
```

测试用agent-memory迁移源码必须与锁定SDK的完整提交一致；没有本地匹配副本时按README准备独立worktree。本轮实际完整执行，未跳过。HTTP复验在持久库留下一个带随机幂等键的合成验证工单；不会改已有工单。

## 范围

P1只持久化support.tickets、support.idempotency_keys、support.ticket_events，原路径与人工完成语义保留。账号/RBAC、真实商品订单、claimed/租约、工作台和推送、性能优化与生产加固，分别属于后续阶段。共享人工token仍是P3前的临时机制，不宣称已支持顾客隔离/座席身份。
