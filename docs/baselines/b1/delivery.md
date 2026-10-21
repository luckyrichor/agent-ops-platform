# P1 补充 B1：PostgreSQL 持久化背景基线

由 Codex 生成 · 2026-10-21（北京时间）· agent-ops-platform@22d7aa914991。测量时源文件哈希与工作区状态详见 results/environment.json，原始样本未修改。新旧方案 SHA256 见 docs/plans/source-2026-10-21.json，旧版完整保留。

## 做了什么

不改 B0 原脚本，包装运行时仅将工单后端设为 PostgreSQL，每轮建立独立数据库、迁移并用非属主账号访问。沿用 B0 的六个 HTTP/SSE 场景和参数：每场景 128 请求、并发 8、预热 10、重复 5。模型、记忆服务与追踪导出均关闭。PG 持久化契约检查 3840/3840 通过。

为避免把跨日期环境变化归因为持久化，另用当前代码的内存开关按同一参数跑五轮，同样 3840/3840 通过。B0 历史标签和原始结果保持。

## 证据

各轮分位数的中位数如下；完整 p50/p95/p99、区间、吞吐及逐请求数据见 [report.json](report.json)、[PG 原始数据](results/raw.json)、[当次内存原始数据](paired-memory/raw.json)。单位 p99=毫秒、吞吐=请求/秒。

| 场景 | 历史B0 p99 | 当次内存 p99 | B1 PG p99 | 当次内存吞吐 | B1 PG吞吐 |
| --- | --- | --- | --- | --- | --- |
| commerce | 28.93 | 38.93 | 62.10 | 539.72 | 471.03 |
| sse | 29.99 | 34.30 | 36.90 | 535.38 | 466.53 |
| ticket_create | 100.20 | 38.86 | 77.20 | 552.18 | 270.47 |
| ticket_replay | 33.78 | 33.41 | 55.40 | 582.82 | 304.51 |
| ticket_conflict | 35.78 | 46.60 | 54.10 | 508.42 | 305.59 |
| ticket_get | 31.99 | 49.52 | 49.86 | 538.48 | 398.37 |

测试 86 passed，10.06 秒，跳过 0；ruff、strict mypy、固定路由评测均通过，日志在本目录。首次测试因迁移源码与固定 SDK 版本不匹配失败；随后改用已存在的干净 1e2730c 兼容 checkout 重跑全量，未更换 SDK 或删除测试。

## 与方案及现状的出入

- B0 内存工单容量 256，PG 无此容量上限；每轮只有 129 个唯一工单，不触发内存淘汰，不能据此声称高容量能力。
- 为保留原脚本 CPU 放置，PG 与 API 共用非发压核心，没有伪称三者独立绑核。原有常驻容器仍运行；短窗口和环境噪声限制比较，不能把某项偶然变快写成优化。
- server_cpu_seconds/RSS 仅 API 进程，不含数据库；数据库资源增量未单独归因。P0/P1 已实现，不按方案中的旧“内存现状”重写。
- 本结果只作 S1、S2 背景对照，不是优化结论。

## 未做与需要决定的事

未做长稳态容量测量、真实模型测量、S1/S2 优化、P3 及以后。B1 无待负责人决定项；按本轮授权，单独汇报后继续 P2。

复现：`uv run python scripts/benchmark-b1.py --output .local/b1-new-run`；全量门使用 `AGENT_MEMORY_SOURCE=/home/ubuntu/project/WorkPlan/agent-memory/.local/compat-1e2730c uv run pytest -q`、`uv run ruff check src tests`、`uv run mypy --strict src`、`uv run python -m agent_ops.evals.support_runner`。
