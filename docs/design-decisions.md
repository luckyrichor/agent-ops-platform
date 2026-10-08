# 设计取舍

最后更新：2026-10-08；Codex；本次代码基线 agent-ops-platform@caa453f + 本轮修改。各历史章节保留原始来源。

M1 先用确定性 planner + 四角色 handler，隔离编排正确性与模型随机性。用户可以观察实际任务依赖、分派与汇总；更换 planner/工具不必重写 DAG executor。代价是没有自由自然语言解析，用户需显式提供预算/类目/地域。真实模型/工具接入后必须另外评估，当前不夸大。

DAG 执行按 ready wave 并发：catalog 后 pricing/shipping 独立，recommendation 依赖二者。plan 在执行前检查 duplicate/unknown/self/cycle，避免挂起；每角色超时，失败不伪造成功，下游 blocked 且独立结果保留。任务异常文本可能有工具正文或凭据，所以只返回固定 error_code。

调用者取消传递到 gather/wait_for 子任务；未做 SSE 背压或高并发压测，取消基础测试不是 M3 的替代。M1 不依赖 memory 可用性，M2 再通过 HTTP SDK 接入，绝不直连它的数据库。
## 2026-10-02 记忆工具与流式取舍

用官方HTTP SDK而非直接访问memory数据库，转发调用者身份而非共享租户凭据。稳定run_id只保证记忆写幂等；409冲突显式传播，不静默制造新key。历史偏好只能收紧当前预算。每请求容量2队列和入队超时让慢消费者局部取消；关闭生成器必须join producer与工具任务，实际HTTP断开测试验证取消链。流的是运行状态，不声称LLM token或完整trace。见memory-streaming.md。此前章节为历史记录。


## 2026-10-07 Codex：W8–W10

为每个dispatcher注入Tracer而不替换全局provider，便于隔离测试和独立运行；请求root在MemoryCommerce执行周期内关闭，适用于SSE任务。工具异常仅ERROR与稳定code，不使用默认异常事件。TaskGroup保证取消后所有并行角色收尾完成。跨服务传播和部署追踪后端后续验证。


## 2026-10-08：真实模型参与规划

需要在线模型调用来验证延迟与故障对编排的影响。先采用受约束 JSON DAG（四个已注册角色），模型不能修改预算、商品事实或权限；本地验证后再调工具。固定 planner 保留作离线基线，显式 llm 模式失败时不静默回退，从结果元数据可区分路径。总预算包含排队与退避，取消传播至 HTTP；共享客户端由应用 lifespan 关闭。当前 key 的 mini 模型未开通，在线初验阻塞，代码通过 mock/集成测试并不等于真实模型调用已验收成功。


## 2026-10-08：失败树、取消和终止通知

未执行的 blocked 任务仍生成 agent span，记录 task_id 与直接 blocked_by，不生成伪造的 tool span。阻塞节点不单独标 ERROR，实际失败工具及业务请求才标 ERROR，避免把一次依赖失败计成多个执行故障。

CancelledError / GeneratorExit 记录 cancelled 与稳定原因码，并继续传播、等待子任务收尾，不记录异常事件或设置 ERROR。HTTP 使用纯 ASGI 中间件保持 context 跨完整响应周期，增加 commerce.http 外层 span；成功体、业务错误体、SSE started/error/aborted/result 与 X-Trace-Id 头关联同一 trace。响应开始后无法修改 HTTP 状态，SSE 用终止事件报告。

终止事件使用队列之外的单个槽位，保证容量 2 的队列堵塞时也能记录 aborted/SLOW_CONSUMER 并及时取消工作，不再等待向满队列入队。消费者恢复读取后先读完最多两条排队帧，再读终止帧；已断开的连接无法保证通知送达。队列超时与业务 TimeoutError 区分，后者属于 RUN_FAILED。未改变模型、安装软件或启动常驻服务。

模型权限说明：此前 mini 未开通为历史记录；截至 caa453f，mini 与 2.1-lite 都已完成真实调用 3/3 初验，默认仍为 2.1-lite。


## 2026-10-08 第二轮：HTTP 分类、构建与双向降级

统一 RequestValidationError/StarletteHTTPException JSON，让 422 与框架错误也返回 trace_id；校验详情不回显 input/ctx。commerce.http 用 SERVER kind，4xx 留 UNSET、5xx ERROR；业务内部 span 不直接充当 HTTP 服务端错误率，SSE 的业务拒绝另记录 application.response.status_code。

采用 PEP508 HTTPS Git 固定提交替代 editable sibling 路径，wheel 依赖元数据也保留来源，防止从 PyPI 获取同名包。已在无同级仓库目录独立安装、构建 wheel/sdist 和启动 CLI；仍依赖完整 agent-memory Python 包，首次取依赖需 Git/网络，轻量 SDK 拆包未做。数据库联调需要迁移源码，因此单独设置 AGENT_MEMORY_SOURCE；默认独立测试明确 skip 该用例，工作区完整门不跳过。

记忆读、写状态与原因码分开，聚合状态新增 read_write_degraded/search_write_degraded；旧单阶段状态保持，不通过“最后一次错误”覆盖先前错误。UUID 误判根因机制与修复证据由 agent-memory@bb09ed0 提供，ops 用固定数字 UUID 经真实 API/PostgreSQL 回归验证；旧随机失败没有正文，不能声称还原了当时具体字符串。
