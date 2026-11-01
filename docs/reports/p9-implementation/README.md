# P9 压测基础设施（已实现，负载未开跑）

2026-11-01，Codex。范围按已批准的 P9 方案：100 → 500 → 1000（可选 2000），取消 5k/10k；模型桩、SSE 负载、故障注入、JSON 证据与报告工具。**尚未执行任何档位的负载**，因为前置换页闸未通过（见 [preflight](p9-preflight/20261101T122937Z-rerun/README.md)）。

## 新增代码

| 文件 | 作用 |
| --- | --- |
| `src/agent_ops/bench/acceptance-v1.json` | 冻结的验收口径：前置闸、校准区间、档位时长、波动上限、资源预算 |
| `bench/stats.py` | 分位数、spread、Wilson、配对 bootstrap 比值、CDF 距离 |
| `bench/faults.py` | 按 request_id + seed 的确定性故障与故障窗；注入率不是供应商真实故障率 |
| `bench/stub.py` | DeepSeek-compatible 本地桩（asyncio 原生 HTTP）：经验 CDF 延迟、429/5xx/重置/断头/截断响应 |
| `bench/planner.py` | bench 专用 planner：同一请求体与计划校验，仅放宽 loopback http 端点 |
| `bench/app.py` | 隔离 bench API：复用 `create_app`，模型指向桩；`/bench/hold` 为传输长连接 fixture |
| `bench/loadgen.py` | 仅标准库负载程序（Mac 运行）：固定并发、ramp、真实 SSE、按字节限速的慢读 |
| `bench/watchdog.py` | 逐秒采样；内存安全线、MemoryHigh、OOM 硬闸 |
| `bench/schema.py` | 版本化运行证据：必填字段、公开 JSON 禁主机/凭据、拒绝覆盖、三次聚合判定 |
| `scripts/p9-preflight.py` | 600 秒前置闸与逐分钟统计 |
| `scripts/p9-calibrate.py` | 真实 DeepSeek 编排 profile 拟合 + 独立留出验证 |
| `scripts/p9-run-tier.py` | 单次运行编排（桩 + bench API + 看门狗 + Mac 生成器） |
| `scripts/p9-report.py` | 三次运行合并判定与 Markdown |
| `scripts/p9-bench-db.sh` | 独立 bench 库、真实迁移、只读复制 P2 目录（20 万商品） |

## 隔离与诚实边界

- **库**：`agent_ops_bench` 独立数据库，属主是运行角色；只从 `agent_ops_p2_seed42` 复制 `commerce.categories/products/inventory`，不复制订单、工单、账号、支付、会话。
- **鉴权**：bench 用 `baseline_without_auth` 的显式 fixture，报告标注“免认证”，不当作线上吞吐。令牌 TTL 与长连接冲突也是选择该 fixture 的原因之一。
- **记忆**：未配置顾客记忆环境变量，`Shopping` 走无记忆分支；本轮不含记忆读写与检索延迟。
- **模型**：planner 指向本地桩，桩不代理真实 API；真实调用只发生在校准。
- **生成器**：Mac（Tailscale 100.x）独立发压，不再与服务同机抢 CPU；tx 只跑 SUT 与桩。

## 校准口径

真实 DeepSeek（`deepseek-flash`，编排 prompt、`max_tokens=800`、温度 0）500 条拟合 + 500 条独立留出；并发 ≤2、每请求 ≤3 次、总调用含重试 ≤1200、预算 ≤2 美元。桩按 request_id 从经验 CDF 取分位并扣除实测 HTTP 开销。判定：p50 比值 95% CI ⊂ [0.90, 1.10]，p95 ⊂ [0.85, 1.15]，CDF 最大差 ≤ 0.10。

## 校准结果（2026-11-01）

真实 DeepSeek `deepseek-flash` 编排 prompt，500 拟合 + 500 独立留出，共 1000 次 HTTP 尝试（0 重试），355,972 token，账单未核对。客户端所见完整延迟：真实验证集 p50 920.6 ms / p95 1198.6 ms；桩 p50 894.0 ms / p95 1128.2 ms。桩自身 HTTP 开销中位 2.03 ms，已在拟合 sleep 中扣除。

| 判定 | 结果 | 区间 |
| --- | --- | --- |
| p50 比值 | 0.971 | 95% CI [0.933, 1.004] ⊂ [0.90, 1.10] |
| p95 比值 | 0.941 | 95% CI [0.880, 0.970] ⊂ [0.85, 1.15] |
| 经验 CDF 最大差 | 0.090 | ≤ 0.10 |

结论：**校准通过**。证据 `docs/reports/p9-calibration/20261101T124404Z/summary.json`，原始逐条延迟与响应仅 `.local/p9-calibration/`。

## 低档冒烟（功能演练，不是验收数据）

20 连接、warmup 5 秒 + ramp 2 秒 + 稳态 10 秒 + drain 3 秒，Mac 直连 bench：20/20 建立，412 次请求全部完成，0 错误；首帧 p95 53.1 ms，全链路 p50 935.2 ms、p95 1186.2 ms。说明桩 → bench → P2 目录 → SSE 全链路可用；该数据不进入三档三次矩阵。

## 前置闸 v2（负责人 2026-11-01 批准）

v1 在 2026-11-01 重跑仍失败（换入非零 93 秒、换出非零 12 秒、换入 4,416 KiB、换出 1,704 KiB），负责人知晓结果后批准另立 v2，未修改 v1。v2 阈值：换出非零秒 ≤5% 且累计 ≤4 MiB；换入非零秒 ≤25% 且累计 ≤8 MiB；单分钟换入非零 ≤50%；MemAvailable ≥2 GiB；memory PSI full <1%。

v2 重跑（13:09–13:19 UTC）：换入非零 6 秒（1.0%）、换出非零 2 秒（0.33%）、换入 1,472 KiB、换出 12 KiB、单分钟峰值 2 秒；MemAvailable 最低 5,153 MiB、PSI full 0.02%。**全部通过**，证据 `docs/reports/p9-preflight/20261101T130929Z-rerun/`。

## 运行完成条件（开跑前固定）

`completed` 需同时满足：实际并发达到目标、健康首帧达到目标、无保护闸中止、致命错误（超时/SSE error/5xx/传输错误）不超过 1%。任一项不满足记 `partial` 并保留原数据，不重跑凑通过。

## 尚未完成

- 负载三档与三次波动验收（被前置闸阻塞）。
- 传输 fixture 与业务路径分开的两套矩阵。
- 高频故障注入实验（属于 P10/S4，不在本轮）。
