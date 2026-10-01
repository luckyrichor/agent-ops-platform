# 设计取舍

最后更新：2026-10-01；Codex；agent-ops-platform@216d302 + 未提交修改。

M1 先用确定性 planner + 四角色 handler，隔离编排正确性与模型随机性。用户可以观察实际任务依赖、分派与汇总；更换 planner/工具不必重写 DAG executor。代价是没有自由自然语言解析，用户需显式提供预算/类目/地域。真实模型/工具接入后必须另外评估，当前不夸大。

DAG 执行按 ready wave 并发：catalog 后 pricing/shipping 独立，recommendation 依赖二者。plan 在执行前检查 duplicate/unknown/self/cycle，避免挂起；每角色超时，失败不伪造成功，下游 blocked 且独立结果保留。任务异常文本可能有工具正文或凭据，所以只返回固定 error_code。

调用者取消传递到 gather/wait_for 子任务；未做 SSE 背压或高并发压测，取消基础测试不是 M3 的替代。M1 不依赖 memory 可用性，M2 再通过 HTTP SDK 接入，绝不直连它的数据库。
