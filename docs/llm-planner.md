# 大模型任务规划接入（2026-10-08，北京时间）

## 目的与边界

该项目要验证真实模型延迟、限流、失败和取消传播，仅有确定性 planner 无法提供这部分证据。本次实现可选的火山 Ark Chat Completions 调用，让模型生成受约束的电商任务 DAG，再交给现有 Dispatcher 执行。

模型仅返回 `catalog / pricing / shipping / recommendation` 固定四角色 DAG 的表示（任务列表顺序可变，依赖集合固定），不生成商品事实，不决定价格或越过预算，不授予权限，也不执行任意工具。每种角色恰好一次，task_id=agent；catalog 是 pricing/shipping 的唯一依赖，pricing/shipping 是 recommendation 的完整依赖集合，不允许额外边。服务端拒绝额外字段、未知角色、缺少或额外依赖、重复 ID/依赖和循环。模型不能改原始请求中的预算、地区和类别；memory 读取仅能收窄预算。

这不是四个自由行动的大模型 Agent：目前调用模型的是规划器，四个业务角色仍为本地代码工具，商品目录和运费仍是演示数据。更开放的工具选择及生产订单能力未实现。

## 运行

凭据只存服务器项目 `.local/ark.env`（0600，gitignored）。程序只从环境取值，不自动扫描/加载文件。当前文件保存用户指定的 key、llm 模式和 Doubao-Seed-2.1-lite 模型，不会进入 GitHub/ZIP。启动示例：

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
| AGENT_OPS_LLM_MODEL | doubao-seed-2-1-lite-260915 |
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
- 首次尝试历史记录：用户指定 key 调用 2.0 mini/lite 返回 HTTP404 / ModelNotOpen，其他两个旧模型名返回 NotFound。原始失败证据保留在 `measurements/llm-mini-blocked-2026-10-08.json`。随后用户开通 Doubao-Seed-2.1-lite，使用同一 key 调用 `doubao-seed-2-1-lite-260915`，**三个真实用例全部成功**。
- 可复现脚本：`source .local/ark.env` 后运行 `uv run python scripts/verify-live-llm.py`；包含预算内商品、运费超预算、空目录三项。遇到首个阻塞即停止，落盘固定错误码和源码 SHA256；不写凭据/原始响应。当前证据见 `measurements/llm-live-2026-10-08.json`，**3/3 成功**；每个用例 planner_mode=llm、planner_model=doubao-seed-2-1-lite-260915，四个工具任务完成。预算内选择 kettle-basic，运费超预算及空目录均 selected=null。

## 当前状态

用户已开通 Doubao-Seed-2.1-lite，本次目标模型权限阻塞已解决。三个构造场景属于真实调用工程初验，不能推断生产质量、性能或付费额度的长期可用性。模型 ID 根据[官方模型列表](https://docs.volcengine.com/docs/82379/1330310?lang=en)核对。

当前已直接声明 OTLP HTTP exporter 依赖，Jaeger 持久化后端已经部署（见 tracing-backend.md），业务 API 未常驻。2.1-lite 与 mini 的真实调用初验都通过，重复评测见下文。此前验收数字对应原版本，不代表当前测试数量。


## 2026-10-08 mini 模型再次复验

用户开通 mini 后，首次复验仍返回 ModelNotOpen（0/3，见 `measurements/llm-mini-recheck-2026-10-08.json`）。稍后用同一指定 key 直接调用已返回 HTTP 200，再运行应用端到端验证，`doubao-seed-2-0-mini-260428` 的预算内推荐、运费超预算和空目录三个构造场景 **3/3 成功**；模型计划经过本地契约校验，四角色工具任务完成。成功证据见 `measurements/llm-mini-live-2026-10-08.json`，包含实测时间及源码 SHA256，对应代码基线 99ff2aa；本轮只补充记录，未修改程序代码。

mini 与 2.1-lite 均已真实验证可用。默认模型及服务器私密配置继续使用 `doubao-seed-2-1-lite-260915`；如需使用 mini，在 source 私密环境后设置 `AGENT_OPS_LLM_MODEL=doubao-seed-2-0-mini-260428`。两个模型的报告分开保存，首次失败记录保留。不将三个构造场景外推为生产质量或长期可用性，未启动常驻服务。


## 固定图的用途与关键路径开销

合法拓扑只有固定契约；调换任务列表不改变 Dispatcher 的依赖调度。自然语言正文不由业务工具解析，工具读取显式 category/budget_cent/region。此阶段不能声称“模型自主规划”，可用于展示共享HTTP、限流/超时/取消、计划验证与trace。一般业务请求优先 deterministic；只有研究真实provider行为时显式启用llm。无需缓存同一张图来避免开销，直接跳过模型更简单；缓存会掩盖真实延迟和限流，不用于在线评测。

```bash
# 即使当前 shell 曾 source 私密 llm 环境，也可显式选择固定图
AGENT_OPS_PLANNER=deterministic uv run python -m agent_ops
# 重复在线评测：3种请求各10次，30次真实规划，单次尝试、串行
source .local/ark.env
uv run python scripts/evaluate-live-planner.py
```

评测只测plan阶段，不执行工具/记忆写入；每个样本都经严格契约验证，并对照确定性图。记录合法计划率、无效计划率、实际HTTP状态/429比例、全部尝试/合法样本延迟p50/p95/p99与确定性规划耗时。用 nearest-rank 分位数；30样本的p99等同最大值。单次尝试让限流不被重试掩盖，不能与默认最多3次重试的端到端耗时混算。不保存提示词、原始响应或凭据；0次429只表示本次没观测到，不估计长期概率，不评估自由规划/自然语言理解或生产质量。报告见 measurements/llm-repeated-2026-10-08.json。

## 旧422记录的追查结论

已在 agent-memory@bb09ed0 确定性复现并修复数字UUID片段被银行卡正则误拒绝；ops@25abaac 使用该固定依赖与数字run_id完成真实数据库联调。旧偶发失败正文没有保留，无法还原其具体随机输入，不将一致的机制误称为已证明那次请求的唯一原因。旧进度章节保留发生时的“未定位”，最新结论见 docs/progress.md 第二轮评审记录。

### 本次重复评测结果（2026-10-08）

模型 doubao-seed-2-1-lite-260915：30/30合法，无效计划0/30、HTTP429为0/30；HTTP200共30次。规划延迟p50=1913.643ms、p95=2703.883ms、p99/最大值=3553.229ms。确定性同请求规划p50=0.012ms。每类10次、单尝试、并发1，全部样本保存在安全报告中；不混算旧三场景端到端耗时。本轮离线全量66 passed in4.31s，ruff、strict mypy10文件通过。
