# S3短测量闸门原始证据（2026-11-04）

- measurement-gate.json / host-metrics.json：第一次执行，DB事件/持有时间跨轮累积，不能作单轮DB层指标；逐请求时延仍有效，原样保留。
- measurement-gate-r2.json / host-metrics-r2.json：CodeBuddy重置单轮计数后复跑，3相×3轮。pool_acquire字段误命名，实际checkout至checkin持有时间，不能当连接获取等待。
- measurement-gate-codex.json / host-metrics-codex.json：Codex修正命名、精确Unix窗口、防覆盖等后的独立复跑，3相×3轮；最终源码之后仅清理行尾空白。
- executed-scripts.json：三批实际执行源码全文与SHA256（JSON转义保存精确字节）；分别匹配raw的fingerprint.script_sha256，不用最终源码冒充旧执行版本。
- codex-protection-controller.py.txt / codex-protection.json：独立运行控制器快照与实时guard原始记录；仅可停止自身单元。
- codex-verification.json：Codex对r2与独立批次18窗口/2880请求的分位数、SQL计数、错误、逐请求比例及历史326文件hash核验结果。

数字解释、首批勘误、所有轮次与未验证项见../../optimizations/S3-measurement-gate-2026-11-04.md。基线是原产品有索引DB方法，合成20万商品，30固定查询，低并发2；无真实查询分布、无模型/记忆网络调用。驱动CPU100%/768MiB/无swap限额不代表共享PG整机限额；健康GET不是业务p95，memory保护入口缺口仍保留。未改其他服务或容器。

复跑：`uv run python scripts/s3-measurement-gate.py --phases product,catalog,shopping --rounds 3 --out <新文件>`，拒绝已有输出；需配合受限systemd单元与保护采样器（控制器快照提供本轮精确命令，复跑需换输出/stop路径）。DSN只从私有bench.url或环境读取，不含凭据。SHA256SUMS覆盖本目录除自身外全部证据。
