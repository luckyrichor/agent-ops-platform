# 大模型任务规划接入（2026-10-08，北京时间）

## 目的与边界

该项目要验证真实模型延迟、限流、失败和取消传播，仅有确定性 planner 无法提供这部分证据。本次实现可选的火山 Ark Chat Completions 调用，让模型生成受约束的电商任务 DAG，再交给现有 Dispatcher 执行。

模型仅提出 `catalog / pricing / shipping / recommendation` 四个任务的顺序和依赖，不生成商品事实，不决定价格或越过预算，不授予权限，也不执行任意工具。每种角色恰好一次，task_id=agent；catalog 为 pricing/shipping 的必要输入，pricing/shipping 为 recommendation 的必要输入。服务端拒绝额外字段、未知角色、缺少输入、重复 ID/依赖和循环。模型不能改原始请求中的预算、地区和类别；memory 读取仅能收窄预算。

这不是四个自由行动的大模型 Agent：目前调用模型的是规划器，四个业务角色仍为本地代码工具，商品目录和运费仍是演示数据。更开放的工具选择及生产订单能力未实现。

## 运行

凭据只存服务器项目 `.local/ark.env`（0600，gitignored）。程序只从环境取值，不自动扫描/加载文件。当前文件保存用户指定的 key、llm 模式和 mini 模型，不会进入 GitHub/ZIP。启动示例：

```bash
source .local/ark.env
uv run python -m agent_ops
uv run uvicorn agent_ops.api:create_app --factory --host 127.0.0.1 --port 8087
```

未配置 `AGENT_OPS_PLANNER` 时默认 deterministic，便于离线评测；配置 llm 后必须有凭据，否则启动失败，不静默改走固定规划器。

| 环境变量 | 默认值 / 意义 |
|---|---|
| AGENT_OPS_PLANNER | deterministic；设置 llm 启用真实请求 |
| ARK_API_KEY | 服务器环境注入，不回显 |
| AGENT_OPS_LLM_MODEL | doubao-seed-2-0-mini-260428 |
| AGENT_OPS_LLM_ENDPOINT | https://ark.cn-beijing.volces.com/api/v3/chat/completions |
| AGENT_OPS_LLM_TIMEOUT_SECONDS | 15，总预算（含并发排队、重试等待、响应读取），最多 60 |
| AGENT_OPS_LLM_CONCURRENCY | 8，每个服务进程的模型请求并发上限，最多 64 |

共享 HTTP 客户端、连接池与并发信号量由 planner 持有，FastAPI lifespan / CLI finally 关闭；取消会传到 HTTP 和后续工具任务。最多尝试 3 次，只重试 408/429/5xx/网络错误，指数等待，并遵循数值 Retry-After；超过总预算返回 LLM_TIMEOUT。401/403 是配置故障，404 是模型不可用，其他永久错误与无效计划不重试。响应限制 64 KiB。HTTP 的 Retry-After 日期形式暂未解析；全局配额和集群限流尚未实现。

HTTP 成功结果包含 `planner_mode / planner_model`，可分辨是否真实模型计划；失败返回 503 和固定错误码，SSE 返回 error 事件和同一原因码，不伪装成成功推荐。模型计划是内部结构 JSON，本次不是模型 token 流式输出，现有 SSE 在调用期间发心跳，完成后发结构化结果。

调用树增加 `commerce.request → planner.llm`，随后进入现有 agent/tool spans；记录模型名和固定失败码，不记录请求正文、模型响应、key 或异常全文。memory 的调用者 JWT 不会发送给模型。

官方接口参考：[Chat API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh&redirect=1)、[结构化输出](https://docs.volcengine.com/docs/ark/structured-output-beta?lang=zh)。模型输出始终在本地重新验证，不以 JSON 模式替代权限与契约检查。

## 验收结果

- 本地 `pytest`：**40 passed in 3.73s**，ruff 通过，strict mypy **10 文件**通过。
- 新增测试覆盖真实应用 API/SSE 路径（MockTransport）、角色契约拒绝、429 后成功、永久失败、耗尽重试、总超时、HTTP 取消、并发上限、共享客户端和安全 trace。
- memory 集成测试按 agent-memory 新权限显式授予 `memory:archive`；未给生产调用者增加权限。
- 首次全量运行受同一 shell 已启用 llm 模式影响，离线测试意外走线上失败路径；新增 autouse fixture 强制离线默认并移除测试 key，模型测试显式注入 mock。随后全量通过。
- 真实调用使用本次用户指定 key：mini 和 lite 返回 **HTTP 404 / ModelNotOpen**，另外两个探测名返回 InvalidEndpointOrModel.NotFound。因此 **真实成功调用验收尚未通过**，不能称在线模型已可用，也不能仅凭这一结果判定 key 无效。
- 可复现脚本：`source .local/ark.env` 后运行 `uv run python scripts/verify-live-llm.py`；包含预算内商品、运费超预算、空目录三项。遇到首个阻塞即停止，落盘固定错误码和源码 SHA256；不写凭据/原始响应。当前证据见 `measurements/llm-live-2026-10-08.json`，0/3 成功，模型权限阻塞。

## 需要用户完成

在火山方舟控制台，为该 key 所属账号开通 `doubao-seed-2-0-mini-260428` 的调用权限与可用额度，或提供已开通的模型 ID / 推理接入点 ID。完成后重跑三项真实验收，实际通过后再更新记录。

本项目未新增直接依赖；锁文件和 .venv 已同步 agent-memory 的新 OTLP 传递依赖。未安装新数据库/容器服务，也未启动常驻 API。程序代码已具备真实调用能力，但当前凭据的目标模型权限仍阻塞在线运行。
