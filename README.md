# agent-ops-platform

最后更新：2026-10-27；Codex；当前能力以本仓库提交及下列验收记录为准。

当前：P3 已验收并提交推送 `8900974`。P4 客服闭环已实现，两库已到 0005，最终验收证据见 [P4 交付](docs/reports/p4-2026-10-27/delivery.md)，接口与取舍见 [P4 文档](docs/p4-support.md)。P4 等待负责人验收，P5 尚未开始；本轮 P4 工作区修改尚未提交推送。导购记忆继续 not_configured（P7 前缺口）。

W4 M1 骨架与电商多 Agent 编排最小闭环已实现。Python 3.12 + FastAPI；确定性规划器将请求分为 catalog、pricing、shipping、recommendation 四个角色任务，依赖校验后分派，pricing/shipping 并发，最终汇总预算内推荐。不同角色是代码中的独立 handler。现已增加可选真实 LLM 规划器，已使用用户开通的 Doubao-Seed-2.1-lite 完成初验及30次重复规划工程评测，详见 [接入与验收](docs/llm-planner.md)。

```bash
bash scripts/bootstrap.sh
uv run python -m agent_ops
# PostgreSQL 默认运行方式（先完成栈初始化与演示账号配置）
uv run python scripts/serve.py --backend postgres --port 8087
uv run pytest -q
uv run ruff check src tests
uv run mypy --strict src
```

HTTP：POST /v1/commerce/runs，JSON 输入 `request`、`category`、`budget_cent`、`region`；输出 request_id、tasks（角色/依赖/状态/结果）、dispatch_order、recommendation。例子：`{"request":"预算50元选上海的水壶","category":"kettle","budget_cent":5000,"region":"上海"}`。CLI 同一请求会选择 3990 分的基础水壶。

任务失败以错误码报告，下游 blocked，其他独立角色保留结果；角色 5 秒超时，调用者取消向正在执行的 handler 传播。没有符合预算商品时成功返回 selected=null 与明确 reason_code。

M2 官方 memory SDK 集成与 M3 SSE/背压/取消已通过本地自动验收；W6 维护接入检索 SDK。P3 后 HTTP 导购必须先以 customer 登录；平台令牌不会转发到 agent-memory。现有 memory 协议不支持服务凭据加平台用户 ID，因此 HTTP 入口暂不调用记忆，状态为 not_configured；P7 前需解决，详见 docs/auth.md。底层独立 SDK 集成测试仍保留。SSE 入口 POST /v1/commerce/stream。完整协议、运行与验证见 [docs/memory-streaming.md](docs/memory-streaming.md)。

覆盖边界：本地固定商品/运费工具；支持确定性及受约束 LLM 计划，Doubao-Seed-2.1-lite 已通过 3 项真实调用初验，不声称自由规划或生产目录/订单能力。没有持久 run 存储、生产身份服务或分布式 worker；M4 trace调用树本地验收通过，见 [调用树](docs/call-tree.md)，TX 已部署持久化 Jaeger/OTLP 后端，见 [部署与访问](docs/tracing-backend.md)；跨服务传播未部署。M5评测/客服子方向尚未完成。写幂等仅覆盖 memory 写入，不保证所有工具 exactly-once。

对应岗位 05/07/10/11/14，另含 08 客服子方向；这些是目标关联，不代表单次演示覆盖完整岗位要求。memory 通过独立 HTTP API 集成，仅安装/导入其 SDK、契约与领域类型。

## 可选大模型调用

服务器凭据保存于 gitignored `.local/ark.env`。`source .local/ark.env` 后运行上述 CLI / API 启用 llm 模式；正常结果返回 planner_mode/planner_model，模型失败不会默默降级为固定计划。日常固定图可用 `AGENT_OPS_PLANNER=deterministic` 避免模型延迟/费用；LLM模式用于验证真实调用行为，不能证明自主规划。模型名、预算、重试与配置详见 [docs/llm-planner.md](docs/llm-planner.md)。默认不加载本地凭据，离线测试固定使用 mock。


## 2026-10-08 追踪与流式完善

HTTP 响应在开始时提供 `X-Trace-Id` / `X-Trace-Sampled`，成功/错误 JSON 和 SSE 事件携带 `trace_id` / `trace_sampled`。未采样时关联号不会出现在追踪后端；采样也不保证投递成功。失败依赖的下游节点记录 `blocked_by`，未执行不伪造工具调用；主动取消记录 cancelled，不当作故障。慢消费者恢复读取后收到 `aborted` / `SLOW_CONSUMER`；断开的连接无法保证通知送达。当前契约见 [调用树](docs/call-tree.md) 和 [记忆与流式](docs/memory-streaming.md)，原始测量及历史测试数量保留原版本来源。


## 独立安装与 2026-10-08 第二轮完善

解压或单独 clone 本仓库后可运行 `bash scripts/bootstrap.sh`，不需要同级 agent-memory 目录。依赖通过 HTTPS Git 固定到 agent-memory@bb09ed0693d23aa6e8538c0cee2cdfe807cf00ce（含 UUID 内容策略修复），pyproject 与 uv.lock 一致；首次安装需要 Git、网络及该仓库读取权限，仍安装完整 agent-memory Python 包，尚未拆成轻量 SDK 发布。wheel / sdist 已在无同级目录的临时位置构建通过，CLI 成功。

默认本地单测与 ASGI/真实 TCP 测试可独立运行；实际数据库联调另需 agent-memory 源码中的 Alembic 迁移。可用 `AGENT_MEMORY_SOURCE=/path/to/agent-memory uv run pytest -q` 指定源码，测试运行时检查源码与安装包版本一致；未指定且同级仓库不存在时，仅该数据库联调用例明确 skip，不将它算通过。当前工作区完整测试 **58 passed**；隔离安装测试 **57 passed, 1 skipped**。

422 校验响应 JSON 提供 trace_id 和字段/type/msg，不回显 input/ctx；404 等框架 HTTP 错误也携带 trace_id。commerce.http 为 SERVER span，记录 http.response.status_code；4xx 不标 ERROR，5xx 标 ERROR，符合 [OpenTelemetry HTTP 语义](https://opentelemetry.io/docs/specs/semconv/http/http-spans/)。内部 memory 工具失败仍可标 ERROR，不混算 HTTP 服务端错误率。

结果新增 memory_read_status、memory_write_status 与对应 error_code；读写同时降级为 read_write_degraded（向量搜索降级加写失败为 search_write_degraded），避免覆盖读失败。未配置两者为 not_configured；业务失败未尝试写为 not_attempted，写成功为 succeeded，写失败为 degraded。memory_status 单阶段值兼容旧值，新增组合值；消费端应允许这些新增值。

## M5 客服与决策评测

接口、人工接管、47条标注集及岗位08前端差距见[客服方向](docs/customer-support.md)。


## B0 首次交付（2026-10-18）

用户提供的 [完善方案原件](docs/agent-ops-platform面试级完善方案.md) 已原样纳入仓库。本轮仅按用户明确描述修复问题并建立B0，后续阶段等待审阅；附件P0/P1与用户阶段解释存在差异，不能当成环境栈/PG数据层已完成。

[B0 范围、修复及复现方式](docs/baselines/b0/README.md)。冻结原始`baseline-v0`和修复后`baseline-b0`；旧内存工单与确定性业务图保留。基线测量为loopback实际HTTP/SSE，不调用付费模型，不提供生产容量或路由准确率结论。后续对照使用新输出目录，不覆盖历史。

### agent-memory 后续独立开发后的迁移源码匹配（2026-10-19）

本仓库SDK仍固定`1e2730c`，没有随memory阶段0自动升级。当前邻接agent-memory主分支已加入0008对话事件迁移，与SDK来源不同；测试的源码一致性检查会正确拒绝混用。联调旧SDK时应显式指定它对应的干净源码，例如在工作区根目录：

```bash
git -C agent-memory worktree add --detach .local/compat-1e2730c 1e2730c
cd agent-ops-platform
AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q tests/test_memory_integration.py
```

worktree已存在时复用，不重复创建；运行前核对HEAD为完整固定提交且src/migrations干净。这个约束防止“新迁移+旧SDK”的假联调，不代表现有SDK与扩展后的API已做完整升级验收。B0报告里的原始验收版本不修改。

## P0/P1 环境栈与PostgreSQL续作

[本轮交付报告](docs/reports/p0-p1-2026-10-21/delivery.md) · [操作入口](docs/p0-p1-operations.md)。实际六组件栈已接通，support工单/幂等/审计改用可切换PG适配器，保留内存基线。完整86测试通过、无跳过；规则回归47/47。B0继续作为冻结历史；当次交付时P2尚未实现，当前进度见下节。业务API仍需显式运行，不常驻。


## 最新方案、B1 与 P2（2026-10-21）

[最新版方案](docs/agent-ops-platform面试级完善方案.md)已替换当前入口，旧版完整保留；新旧 SHA256 见 [来源记录](docs/plans/source-2026-10-21.json)。[B1 独立报告](docs/baselines/b1/delivery.md)已先交付并冻结 `baseline-b1`，不作优化结论。

[P2 交付与缺口](docs/reports/p2-2026-10-21/delivery.md) · [数据来源、许可与校验](docs/datasets.md)。商品/库存/订单/物流本地应用服务和全量合成导入已验证，同种子重跑按数据校验和核对；公开统计不含数据正文。代理仅为“规则代理，非人工真实意图分布，首片段说话人未确认”，等待人工比例独立可配，不进路由质量指标；人工权重待 P8 替换。

P2 的 UserBehavior 完整文件、增量拟合与核验已完成，负责人已验收并授权 P3。数据仍在独立 P2 库，主 HTTP 默认配置仍使用 P0 主库。P3 认证、角色和归属隔离已实现；2026-10-26 两个持久库均已到 0004，P2 演示账号已创建。导购暂不调用记忆，状态 not_configured；P4 和 S1–S7 均未开始。详见 [认证与运行](docs/auth.md) 和 [本轮执行口径](docs/p3-followup-plan-2026-10-26.md)。


## P3 认证与权限（2026-10-23）

邮箱密码登录、三种角色、访问/刷新令牌，以及订单/会话/工单的按归属隔离已实现，并有越权测试覆盖；见 [认证与权限](docs/auth.md)。真实库迁移和演示账号创建尚未在服务器库上执行。P4 及以后未开始。
