# agent-ops-platform

最后更新：2026-10-08；Codex；当前能力以本仓库提交及下列验收记录为准。

W4 M1 骨架与电商多 Agent 编排最小闭环已实现。Python 3.12 + FastAPI；确定性规划器将请求分为 catalog、pricing、shipping、recommendation 四个角色任务，依赖校验后分派，pricing/shipping 并发，最终汇总预算内推荐。不同角色是代码中的独立 handler。现已增加可选真实 LLM 规划器，已使用用户开通的 Doubao-Seed-2.1-lite 完成 3 项真实调用初验，详见 [接入与验收](docs/llm-planner.md)。

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

覆盖边界：本地固定商品/运费工具；支持确定性及受约束 LLM 计划，Doubao-Seed-2.1-lite 已通过 3 项真实调用初验，不声称自由规划或生产目录/订单能力。没有持久 run 存储、生产身份服务或分布式 worker；M4 trace调用树本地验收通过，见 [调用树](docs/call-tree.md)，追踪后端与跨服务传播未部署。M5评测/客服子方向尚未完成。写幂等仅覆盖 memory 写入，不保证所有工具 exactly-once。

对应岗位 05/07/10/11/14，另含 08 客服子方向；这些是目标关联，不代表单次演示覆盖完整岗位要求。memory 通过独立 HTTP API 集成，仅安装/导入其 SDK、契约与领域类型。

## 可选大模型调用

服务器凭据保存于 gitignored `.local/ark.env`。`source .local/ark.env` 后运行上述 CLI / API 启用 llm 模式；正常结果返回 planner_mode/planner_model，模型失败不会默默降级为固定计划。模型名、预算、重试与配置详见 [docs/llm-planner.md](docs/llm-planner.md)。默认不加载本地凭据，离线测试固定使用 mock。
