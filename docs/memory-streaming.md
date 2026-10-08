# W5–W7 记忆工具与 SSE

最后更新：2026-10-08（北京时间）；Codex；本次代码基线 agent-ops-platform@caa453f + 本轮修改。W5–W7 原始来源 ed6f887 + 当时工作树；历史测量保留原版本。

## W5 / M2

通过官方 `agent_memory.sdk.MemoryClient` 调 HTTP API。SDK 来源现为固定提交的 HTTPS Git 依赖（agent-memory@bb09ed0），不再使用 `../agent-memory` editable 路径。服务数据库仍独立，平台不 import memory 应用服务或仓储。首次安装仍需 Git/网络及仓库读取权限，未单独发布轻量 SDK。

配置 `AGENT_MEMORY_URL` 后，HTTP 请求必须携带调用者 Bearer token；转发同一身份给 memory，不能使用公共机器人凭据。调用者需有 commerce 工作区权限。未配置服务可独立运行，结果 `memory_status=not_configured`。

时序：可选 memory_id 精确读历史偏好 → 编排商品/价格/运费/推荐工具 → 成功后写 episodic 结果。偏好 JSON 的 budget_cent 只能收紧当前显式预算。写入 key 为 `commerce:<run_id>`，客户端重试必须复用 run_id，同键不同内容返回 409。幂等限定为记忆写入，尚无持久 run 存储，不承诺所有业务工具 exactly-once。

读失败降级到无记忆推荐，写失败保留业务结果；仅写失败返回 write_degraded，读写同时失败返回 read_write_degraded，搜索向量降级加写失败返回 search_write_degraded。memory_read_status/memory_write_status 及各自 error_code 保留完整原因，不给伪造 memory_id。CancelledError 向 SDK HTTP 调用和角色任务传播。memory 是历史上下文，不能覆盖工具权限或执行任意历史文本。

## W6 维持

未提供 memory_id 时调用新 `MemoryClient.search`，最多 3 条 commerce semantic 记忆，响应 memory_hit_count。检索结果不直接改变显式业务参数。检索失败与无命中区分，向量 provider 降级有 search_degraded 标记。

## W7 / M3

`POST /v1/commerce/stream` 接受同样请求；SSE 事件 started → heartbeat → result、error 或 aborted（终止事件）。当前流的是运行状态与最终推荐，未实现 LLM token 流；逐角色 trace 已在 M4 实现，详见 call-tree.md。

每请求独立容量 2 的队列，入队等待上限 2 秒；堵塞只取消该请求。正常流 heartbeat 每 0.1 秒。客户端断开/关闭迭代器时 finally 取消 producer 和所有正在执行的工具，无全局共享出队锁。错误事件含固定原因码和 trace_id（包括模型错误和记忆冲突），不输出内部异常正文。每次响应在开始时提供 X-Trace-Id；started 同时携带 trace_id。慢消费者超时保留 aborted/SLOW_CONSUMER 终止事件，恢复读取后可收到；已经断开的连接无法保证送达。

## 证据与边界

W5–W7 历史验收为 18 pytest passed：包括真实 SDK→memory API→迁移后 PostgreSQL 的读写/重放/冲突/archive 回退；真实 TCP HTTP 服务断开后的取消、16 个并发正常 HTTP 请求；一个未消费迭代器与 32 个正常流的隔离。ruff、strict mypy 8 文件通过。

`uv run python scripts/measure-streams.py` 生成 docs/measurements/2026-10-02-streams.json，包含机器、工作树/源码哈希、每请求耗时。只测单机确定性工具和队列隔离，不是线上模型吞吐、网络慢连接饱和或生产容量。临时服务测试完成后退出。

运行：`bash scripts/bootstrap.sh`；`uv run uvicorn agent_ops.api:create_app --factory --host 127.0.0.1 --port 8087`。全量验收需 tx Docker：`sg docker -c 'uv run pytest -q'`，另跑 ruff 与 mypy。


第二轮：422 校验 JSON 也提供 trace_id；HTTP SERVER 的 4xx 留 UNSET、5xx 才标 ERROR。完整工作区测试 58 passed；无同级源码独立环境 57 passed/1 skipped（唯一跳过的是需要外部 Alembic 源码的数据库联调）。数据库联调固定数字 UUID，确认修复后的服务不误拒绝；见 README 安装说明。
