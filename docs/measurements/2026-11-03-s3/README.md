# S3前置证据（任务始于2026-11-03，实际执行2026-11-04）

事实与勘误见`../../optimizations/S3-prerequisites-2026-11-04.md`，待确认方案见`../../optimizations/S3-design.md`。

- distribution-formal.json：原聚合结果，原样保留；freeze-v1是当时冻结参数。
- protocol-executed.md / distribution-formal-executed.py.txt：实际执行字节快照，SHA与原结果对应；当前脚本修正防覆盖和统计顺序，不冒充当时版本。
- codex-independent-verification.json：Codex从原ZIP核对百万PV并独立复算全部验证/三轮延迟。真实序列不公开，复算需本机原ZIP及私有sample。
- retrieval-bench-stage2-r1..r3.json：原三轮300请求、并发2、预热60，原始逐请求延迟。
- retrieval-bench-codex.json / host-metrics-codex.json / codex-live-verification.json：补充一轮含独立标签指标的复核与准确墙钟采样；较慢尾延迟照实保留。
- refund-bench-codex.json：完整ledger计数和网关记录的独立退款复核；仅合成克隆实验库，不涉及真实交易。
- zh-review-table-synthetic.* / codex-relevance-review.json：固定合成查询的top5表与Codex逐条审阅，不是用户人工标签。

此目录不含DSN/secret/真实用户/请求原文；原始真实PV序列仅.local私有。所有阶段1失败、不同revision探索和旧报告均保留在其既有位置。

- codex-migration-verification.json：空隔离PG数据库完整迁移升/降/升、双索引及真实缺列guard验证，已清理临时数据库。

复跑检索：`uv run python scripts/s3-retrieval-bench.py --requests 300 --concurrency 2 --warmup-per-worker 30 --labels --out .local/s3/new-run.json`（先准备私有bench.url或S3_BENCH_DSN）。退款：`uv run python scripts/s2-refund-bench.py --out .local/s2/new-run.json`。分布新协议须在隔离工作目录准备原ZIP和冻结fit后运行，当前脚本拒绝覆盖本轮冻结文件与结果，不得删除它们来追好结果。
