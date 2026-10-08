# 进度记录

最后更新：2026-10-08（北京时间）

本文件是 `agent-ops-platform` 的进度事实源，汇总到 `workplan-docs/进度总览.md`。

格式：每条记录写明日期、做了什么、验证方式与结果、遇到的问题。**不写计划，只写已发生的事**；失败和返工也要记，那是面试时最有料的部分。

---

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


## 2026-10-08：Doubao-Seed-2.1-lite 真实调用复验（Codex）

用户开通 2.1-lite 后，核对官方 ID doubao-seed-2-1-lite-260915，修改默认模型与服务器 .local/ark.env（key 不变）。使用真实模型通过应用 API 完成 3/3 构造场景：5000 分上海选择基础水壶；4500 分杭州因运费超预算不推荐；unknown 类目为空目录。每例四个角色任务完成，planner_mode=llm。模型负责受约束计划，本地工具仍负责事实与预算，不声称生产评测或所有角色由 LLM 驱动。

新报告 docs/measurements/llm-live-2026-10-08.json 包含实际耗时/模型名/源码 SHA256；旧 mini 阻塞报告保存为 llm-mini-blocked-2026-10-08.json。pytest 40 项、ruff、strict mypy 10 文件重验通过；未启动常驻 API，未公开凭据。


## 2026-10-08 mini 模型再次复验

用户开通 mini 后，首次复验仍返回 ModelNotOpen（0/3，见 `docs/measurements/llm-mini-recheck-2026-10-08.json`）。稍后用同一指定 key 直接调用已返回 HTTP 200，再运行应用端到端验证，`doubao-seed-2-0-mini-260428` 的预算内推荐、运费超预算和空目录三个构造场景 **3/3 成功**；模型计划经过本地契约校验，四角色工具任务完成。成功证据见 `docs/measurements/llm-mini-live-2026-10-08.json`，包含实测时间及源码 SHA256，对应代码基线 99ff2aa；本轮只补充记录，未修改程序代码。

mini 与 2.1-lite 均已真实验证可用。默认模型及服务器私密配置继续使用 `doubao-seed-2-1-lite-260915`；如需使用 mini，在 source 私密环境后设置 `AGENT_OPS_LLM_MODEL=doubao-seed-2-0-mini-260428`。两个模型的报告分开保存，首次失败记录保留。不将三个构造场景外推为生产质量或长期可用性，未启动常驻服务。


## 2026-10-08：追踪与 SSE 五项完善（Codex）

来源 agent-ops-platform@caa453f + 本轮修改；精确源码 SHA256 见 measurements/2026-10-08-reliability.json。blocked 下游生成带 task_id/blocked_by 的 agent span，不伪造 tool span；CancelledError、GeneratorExit 与发送阶段断连按 cancelled 分类，不标 ERROR。HTTP 外层 commerce.http 保持整条响应 context，X-Trace-Id 在响应开始时返回；业务错误 JSON、SSE started/error/aborted/result 都可关联 trace，包含模型故障、记忆 409、未知异常 500。未知异常仍脱敏。

满队列的终止帧使用单独槽位，不再向满队列等待；慢消费者恢复读取后收到 aborted/SLOW_CONSUMER。已断开的连接不能保证通知送达。原容量 2、子任务取消收尾和多请求隔离保留；业务 TimeoutError 不误标慢消费者。文档去掉“尚无记录”，更新当前头部日期/基线，旧日期、提交与测试数字明确保留为历史记录。

最终 pytest **48 passed in 4.13s**；ruff（src/tests/调用树脚本）通过；strict mypy **10 文件**通过。新增 8 项用例覆盖 503/409/500 trace、实际 memory 写冲突 SSE、取消分类、满队列终止、业务超时分类和传输断连；更新原 blocked 树及真实 TCP 取消测试，16 个并发请求仍能完成。新的调用树演示成功导出两条合成 trace，见 measurements/2026-10-08-call-tree.json。初次新增测试受系统 SOCKS 代理且缺 socksio 影响，隔离测试代理环境后通过；之后一次既有 memory 集成回归返回 422，单独复验及最终全量通过，未将该偶发失败隐去，也未宣称已定位其根因。未新增软件/容器常驻服务，未改模型配置或调用付费模型。


## 2026-10-08：第二轮评审与独立构建（Codex）

来源 agent-ops-platform@43969b4 + 本轮修改；精确源码 SHA256 见 measurements/2026-10-08-second-review.json。422 RequestValidationError 和框架 StarletteHTTPException 均携带 trace_id；校验详情保留 loc/type/msg，不回显 input/ctx。commerce.http 改 SERVER kind，http.response.status_code 记录实际状态；4xx 留 UNSET，5xx 标 ERROR；内部工具故障语义保留，SSE HTTPException 的应用 4xx 不标服务端 ERROR。

已确定性复现 agent-memory 内容正则将数字 UUID 片段误判为银行卡，旧版本 sensitive=True，修复后 False。旧偶发失败未保留正文，无法确认它实际命中的具体随机字段；机制与 422 一致，未用“重跑通过”替代定位。agent-memory@bb09ed0 只在数字类检测中排除规范独立 UUID，真实卡号/手机号、密钥词和令牌仍拒绝；memory 四道门192测试及 GitHub Actions #37737890139 全部通过。本项目真实 API/迁移后 PostgreSQL 集成用固定数字 run_id 验证创建、重放、冲突、归档/搜索。

独立安装去掉 ../agent-memory editable 源，PEP508 HTTPS Git 固定至 bb09ed0693d23aa6e8538c0cee2cdfe807cf00ce，uv.lock 与 wheel 元数据同步。无同级 checkout 临时目录 bootstrap、wheel/sdist 构建、CLI 全成功；隔离测试57 passed/1 skipped（迁移源码联调明确跳过，未算通过）。首次取包仍需 Git/网络/仓库读取权限，完整 Python 服务包仍是依赖，未发布轻量 SDK；联调可显式 AGENT_MEMORY_SOURCE，要求源码 HEAD 匹配已安装 SDK 固定版本且 src/migrations 干净。当前工作区全量 **58 passed in 4.10s**，ruff、strict mypy10文件通过，无跳过。

memory_read_status/memory_write_status 和对应错误码分别保留，读写同时失败为 read_write_degraded，向量搜索降级加写失败为 search_write_degraded；原单阶段值保留。已测试四种读写成功/失败组合。未调用付费模型、未改私密凭据或常驻部署。
