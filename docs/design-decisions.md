# 设计取舍

最后更新：2026-10-01；Codex；agent-ops-platform@216d302 + 未提交修改。

M1 先用确定性 planner + 四角色 handler，隔离编排正确性与模型随机性。用户可以观察实际任务依赖、分派与汇总；更换 planner/工具不必重写 DAG executor。代价是没有自由自然语言解析，用户需显式提供预算/类目/地域。真实模型/工具接入后必须另外评估，当前不夸大。

DAG 执行按 ready wave 并发：catalog 后 pricing/shipping 独立，recommendation 依赖二者。plan 在执行前检查 duplicate/unknown/self/cycle，避免挂起；每角色超时，失败不伪造成功，下游 blocked 且独立结果保留。任务异常文本可能有工具正文或凭据，所以只返回固定 error_code。

调用者取消传递到 gather/wait_for 子任务；未做 SSE 背压或高并发压测，取消基础测试不是 M3 的替代。M1 不依赖 memory 可用性，M2 再通过 HTTP SDK 接入，绝不直连它的数据库。
## 2026-10-02 记忆工具与流式取舍

用官方HTTP SDK而非直接访问memory数据库，转发调用者身份而非共享租户凭据。稳定run_id只保证记忆写幂等；409冲突显式传播，不静默制造新key。历史偏好只能收紧当前预算。每请求容量2队列和入队超时让慢消费者局部取消；关闭生成器必须join producer与工具任务，实际HTTP断开测试验证取消链。流的是运行状态，不声称LLM token或完整trace。见memory-streaming.md。此前章节为历史记录。


## 2026-10-07 Codex：W8–W10

为每个dispatcher注入Tracer而不替换全局provider，便于隔离测试和独立运行；请求root在MemoryCommerce执行周期内关闭，适用于SSE任务。工具异常仅ERROR与稳定code，不使用默认异常事件。TaskGroup保证取消后所有并行角色收尾完成。跨服务传播和部署追踪后端后续验证。


## 2026-10-08：真实模型参与规划

需要在线模型调用来验证延迟与故障对编排的影响。先采用受约束 JSON DAG（四个已注册角色），模型不能修改预算、商品事实或权限；本地验证后再调工具。固定 planner 保留作离线基线，显式 llm 模式失败时不静默回退，从结果元数据可区分路径。总预算包含排队与退避，取消传播至 HTTP；共享客户端由应用 lifespan 关闭。当前 key 的 mini 模型未开通，在线初验阻塞，代码通过 mock/集成测试并不等于真实模型调用已验收成功。
