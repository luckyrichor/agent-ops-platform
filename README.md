# agent-ops-platform

最后更新：2026-10-08；Codex；当前能力以本仓库提交及下列验收记录为准。

W4 M1 骨架与电商多 Agent 编排最小闭环已实现。Python 3.12 + FastAPI；确定性规划器将请求分为 catalog、pricing、shipping、recommendation 四个角色任务，依赖校验后分派，pricing/shipping 并发，最终汇总预算内推荐。不同角色是代码中的独立 handler。现已增加可选真实 LLM 规划器，已使用用户开通的 Doubao-Seed-2.1-lite 完成初验及30次重复规划工程评测，详见 [接入与验收](docs/llm-planner.md)。

```bash
bash scripts/bootstrap.sh
uv run python -m agent_ops
uv run uvicorn agent_ops.api:create_app --factory --host 127.0.0.1 --port 8087
uv run pytest -q
uv run ruff check src tests
uv run mypy --strict src
```

HTTP：POST /v1/commerce/runs，JSON 输入 `request`、`category`、`budget_cent`、`region`；输出 request_id、tasks（角色/依赖/状态/结果）、dispatch_order、recommendation。例子：`{"request":"预算50元选上海的水壶","category":"kettle","budget_cent":5000,"region":"上海"}`。CLI 同一请求会选择 3990 分的基础水壶。

任务失败以错误码报告，下游 blocked，其他独立角色保留结果；角色 5 秒超时，调用者取消向正在执行的 handler 传播。没有符合预算商品时成功返回 selected=null 与明确 reason_code。

M2 官方 memory SDK 集成与 M3 SSE/背压/取消已通过本地自动验收；W6 维护接入检索 SDK。配置 AGENT_MEMORY_URL 后转发调用者 Bearer 身份；客户端重试复用 run_id，写记忆使用稳定幂等键。SSE 入口 POST /v1/commerce/stream。完整协议、运行与验证见 [docs/memory-streaming.md](docs/memory-streaming.md)。

覆盖边界：本地固定商品/运费工具；支持确定性及受约束 LLM 计划，Doubao-Seed-2.1-lite 已通过 3 项真实调用初验，不声称自由规划或生产目录/订单能力。没有持久 run 存储、生产身份服务或分布式 worker；M4 trace调用树本地验收通过，见 [调用树](docs/call-tree.md)，TX 已部署持久化 Jaeger/OTLP 后端，见 [部署与访问](docs/tracing-backend.md)；跨服务传播未部署。M5评测/客服子方向尚未完成。写幂等仅覆盖 memory 写入，不保证所有工具 exactly-once。

对应岗位 05/07/10/11/14，另含 08 客服子方向；这些是目标关联，不代表单次演示覆盖完整岗位要求。memory 通过独立 HTTP API 集成，仅安装/导入其 SDK、契约与领域类型。

## 可选大模型调用

服务器凭据保存于 gitignored `.local/ark.env`。`source .local/ark.env` 后运行上述 CLI / API 启用 llm 模式；正常结果返回 planner_mode/planner_model，模型失败不会默默降级为固定计划。日常固定图可用 `AGENT_OPS_PLANNER=deterministic` 避免模型延迟/费用；LLM模式用于验证真实调用行为，不能证明自主规划。模型名、预算、重试与配置详见 [docs/llm-planner.md](docs/llm-planner.md)。默认不加载本地凭据，离线测试固定使用 mock。


## 2026-10-08 追踪与流式完善

HTTP 响应在开始时提供 `X-Trace-Id`，业务错误 JSON 和 SSE 事件也携带 `trace_id`。失败依赖的下游节点记录 `blocked_by`，未执行不伪造工具调用；主动取消记录 cancelled，不当作故障。慢消费者恢复读取后收到 `aborted` / `SLOW_CONSUMER`；断开的连接无法保证通知送达。当前契约见 [调用树](docs/call-tree.md) 和 [记忆与流式](docs/memory-streaming.md)，原始测量及历史测试数量保留原版本来源。


## 独立安装与 2026-10-08 第二轮完善

解压或单独 clone 本仓库后可运行 `bash scripts/bootstrap.sh`，不需要同级 agent-memory 目录。依赖通过 HTTPS Git 固定到 agent-memory@bb09ed0693d23aa6e8538c0cee2cdfe807cf00ce（含 UUID 内容策略修复），pyproject 与 uv.lock 一致；首次安装需要 Git、网络及该仓库读取权限，仍安装完整 agent-memory Python 包，尚未拆成轻量 SDK 发布。wheel / sdist 已在无同级目录的临时位置构建通过，CLI 成功。

默认本地单测与 ASGI/真实 TCP 测试可独立运行；实际数据库联调另需 agent-memory 源码中的 Alembic 迁移。可用 `AGENT_MEMORY_SOURCE=/path/to/agent-memory uv run pytest -q` 指定源码，测试运行时检查源码与安装包版本一致；未指定且同级仓库不存在时，仅该数据库联调用例明确 skip，不将它算通过。当前工作区完整测试 **58 passed**；隔离安装测试 **57 passed, 1 skipped**。

422 校验响应 JSON 提供 trace_id 和字段/type/msg，不回显 input/ctx；404 等框架 HTTP 错误也携带 trace_id。commerce.http 为 SERVER span，记录 http.response.status_code；4xx 不标 ERROR，5xx 标 ERROR，符合 [OpenTelemetry HTTP 语义](https://opentelemetry.io/docs/specs/semconv/http/http-spans/)。内部 memory 工具失败仍可标 ERROR，不混算 HTTP 服务端错误率。

结果新增 memory_read_status、memory_write_status 与对应 error_code；读写同时降级为 read_write_degraded（向量搜索降级加写失败为 search_write_degraded），避免覆盖读失败。未配置两者为 not_configured；业务失败未尝试写为 not_attempted，写成功为 succeeded，写失败为 degraded。memory_status 单阶段值兼容旧值，新增组合值；消费端应允许这些新增值。
