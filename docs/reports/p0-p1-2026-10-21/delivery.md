# agent-ops-platform P0/P1交付报告

日期：2026-10-21（北京时间）；Codex；来源 agent-ops-platform@c1e7253；工作区状态见JSON。

本报告接续首次B0交付，范围按方案第六节的P0/P1；原B0报告与基线保持历史，不覆盖。

## P0 环境栈

一条命令 bash scripts/start-stack.sh，实际启动/复用 6 个服务：PostgreSQL、Redis、Collector、Prometheus、Grafana、Jaeger。镜像按实际拉取摘要固定，配置/数据卷分离；复用原Jaeger与Badger卷。HTTP健康 {'jaeger': 200, 'collector': 200, 'prometheus': 200, 'grafana': 200}。

|组件|实际镜像|内存上限(bytes)|
|---|---|---:|
|collector|otel/opentelemetry-collector-contrib:0.120.0@sha256:85ac41c2db88d0df9bd6145e608a3cb023f5d8443868adbfbbf66efb51087917|268435456|
|grafana|grafana/grafana:11.5.2@sha256:8b37a2f028f164ce7b9889e1765b9d6ee23fec80f871d156fbf436d6198d32b7|268435456|
|jaeger|jaegertracing/jaeger:2.22.0@sha256:836b967b5abcf1f7758066aa6b348619267a2f1459f72d472840570f9bd88a17|805306368|
|postgres|postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea|536870912|
|prometheus|prom/prometheus:v3.2.1@sha256:6927e0919a144aa7616fd0137d4816816d42f6b816de3af269ab065250859a62|268435456|
|redis|redis:7.4-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499|134217728|

主机CPU 4，内核 6.8.0-139-generic，内存 {'MemTotal': '7869640 kB', 'MemAvailable': '5447492 kB'}；CPU配额和实际镜像ID、端口绑定、组件版本见environment.json。这里是环境/连通性验收，不是容量压测，未声称核绑定性能对照。

经Collector真实导出/Jaeger查询 4 类追踪，全部query_status通过；私有探针正文未导出=True。Prometheus Collector up=True，OTLP探针指标可查=True，Grafana数据源=OK。尾部采样、完整业务指标和跨服务传播仍属后续。

## P1 PostgreSQL数据层

Alembic upgrade head 创建support.tickets、idempotency_keys、ticket_events；支持downgrade base/re-upgrade隔离验收。运行角色非owner，只能追加事件；触发器禁止事件UPDATE/DELETE。原工单/人工完成接口保持，未提前实现P4抢单租约。

- 2 个独立OS进程，共 16 次同键写入：工单 1 张，重复工单 0。
- 实际PostgreSQL容器重启后保留=True；并发CAS成功 1，版本/状态冲突 7；当前revision=2，原请求重放仍为初始revision=1。
- 主P0持久库实际TCP HTTP复验：2 个API进程、16 请求、1 张工单；应用进程重启后保留=True，重复工单 0。
- 事件插入故障注入验证事务回滚：工单和幂等记录不留半成品；HTTP切换、trace头、404、人工完成及append-only权限测试均通过。
- 保留原SupportService为memory开关；create_app默认memory，scripts/serve.py默认postgres。PG故障不会自动回退内存。

## 门禁

pytest 86 passed / 10.17s，跳过 0；All checks passed!；Success: no issues found in 15 source files。规则评测 47/47，仍仅为原固定回归集，不当真实路由准确率。

## 运行与边界

```bash
bash scripts/start-stack.sh
uv run python scripts/serve.py --backend postgres
# 独立运行内存基线：
uv run python scripts/serve.py --backend memory --port 8004
```
私密配置由启动脚本生成于.local/stack.env及.local/application.env（目录700、文件600），不进Git/附件、不打印密码。脚本可重复启动，不删除卷。新端口仅loopback；业务API仍需显式运行，不常驻、不暴露公网。
P1重启与跨进程并发测试运行于真实隔离PostgreSQL容器，属于正确性验收，不代表生产容量或故障恢复SLA。首轮重启测试因随机宿主端口变化失败，改用显式测试端口；Jaeger健康检查由旧v1路径修正为现有v2 /status，返工记录保留。
仍未完成：P2商品订单数据、P3账号/RBAC（仍共享人工token）、P4客服领取/租约/消息闭环、后续优化实验与生产备份加固。P0/P1完成不等于整体生产化方案完成。
