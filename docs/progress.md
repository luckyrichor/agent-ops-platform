# 进度记录

最后更新：2026-10-02（北京时间）

本文件是 `agent-ops-platform` 的进度事实源，汇总到 `workplan-docs/进度总览.md`。

格式：每条记录写明日期、做了什么、验证方式与结果、遇到的问题。**不写计划，只写已发生的事**；失败和返工也要记，那是面试时最有料的部分。

---

_尚无记录。_

## 2026-10-01 W2 维持（Codex）

核对 agent-ops-platform@216d302，开工 clean，pull --ff-only 已同步。新增 docs/task-contract.md：确定电商任务角色、依赖、错误结果与独立 memory 服务边界。人工审阅契约与总节奏表 M1/M2 一致；此项是接口设计进展，不是 M1 实现完成。未提交、未推送；未虚构工时。

## 2026-10-01 W4 / M1 自动验收通过（Codex）

来源 agent-ops-platform@216d302 + 未提交修改，tx；与 W2 契约设计独立记账。新增 pyproject/uv.lock、src/agent_ops（models/planner+dispatcher/四角色工具/API/CLI）、tests 与 scripts/bootstrap.sh。一个电商请求实际拆成四任务，pricing/shipping 按依赖并发，预算与库存筛选后汇总结果。

本次 pytest **10 passed in 1.02s**（ASGI e2e、实际并发 rendezvous、失败/blocked、无预算匹配、五种非法图、取消传播）；ruff 通过；mypy strict **6 source files** 通过。CLI `uv run python -m agent_ops` 实跑 succeeded，4 个角色均 succeeded，推荐 kettle-basic / total_cent=3990 / budget_cent=5000。

固定 planner 与本地演示工具，无线上 LLM/bot 请求，不声称自由任务规划；memory 集成仍属 M2，SSE/trace 完整实现未做。未提交/推送，未虚构工时。
## 2026-10-02 W5–W7（Codex）

来源 agent-ops-platform@ed6f887 + 本轮工作树修改，依赖本轮 agent-memory SDK。W5 M2：官方 SDK 的读→工具编排→写时序、读写失败回退、稳定 run_id 写入幂等和冲突传播。W6 独立维护：接入 search SDK 与检索命中/降级状态。W7 M3：每请求有界 SSE 队列、慢消费者取消隔离、断开后取消正在执行的 HTTP/角色工具。

末次本地 pytest 18 passed（含实际记忆 API/PostgreSQL、实际 HTTP 断开、16 并发 HTTP、32 正常流与1慢流），ruff 与 strict mypy 8 文件通过。可复现并发测量在 docs/measurements/2026-10-02-streams.json，具体耗时和源码 SHA256 以该文件为准，不视为生产性能。M2/M3 自动验收通过；M4/M5 不在本轮执行范围。docs/memory-streaming.md 记录接口和边界。无实际工时声明，此前章节为历史记录。


## 2026-10-07 Codex：W8–W10实际执行

W8独立维护：TaskGroup管理ready角色批次，取消时收完pricing/shipping两个并行finally。W10 M4：OpenTelemetry请求根span→Agent→实际工具调用树，memory HTTP读写工具也在同一请求上下文；API返回trace_id，失败标ERROR+原因码，不记录正文/凭证/异常事件。成功与pricing失败两条实测调用树已导出，memory HTTP503降级仍可定位具体hop。当前为本地/控制台导出，不声称部署追踪后端或跨服务traceparent。

22 pytest passed in 7.60s，ruff/strict mypy9文件通过。包含真实memory API/数据库原回归、并行取消、失败工具及memory降级追踪。来源agent-ops-platform@8c041be + 本轮工作树；精确SHA256见测量元数据。说明见docs/call-tree.md。


## 2026-10-08：可选真实 LLM 规划器（Codex）

实现 Ark Chat Completions planner，API/SSE/CLI 共用，模型计划经过四角色白名单、必需输入和无环验证；价格/运费/预算仍由演示工具确定。新增共享 HTTP 池、总时间预算、并发 8、最多 3 次临时错误重试、取消、显式 planner 元数据、失败原因和安全 span。未新增直接依赖；锁文件/.venv 同步 agent-memory 新传递依赖，未启动常驻 API。

本地 pytest 40 passed in 3.73s，ruff 通过，strict mypy 10 文件通过，含实际 memory API + PostgreSQL 集成。旧集成 JWT 缺新 memory:archive 权限，更新测试显式授权；生产授权未改变。首次测试 shell 启用 llm 影响离线测试，增加 fixture 清理 key/模式后重验。

真实使用用户本次指定 key：mini/lite HTTP404 ModelNotOpen；另外两个探测名 HTTP404 InvalidEndpointOrModel.NotFound。真实三案例脚本第一个用例即阻塞，0/3 成功。源码校验和与安全错误证据在 measurements/llm-live-2026-10-08.json，不伪造线上成功。需要账号开通目标模型或提供有效接入点后重验。此前 M1–M4 验收记录为历史版本，不能替代本次在线模型验收。
