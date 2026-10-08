# 追踪后端：部署与访问

最后更新：2026-10-08（北京时间）；Codex；代码基线 agent-ops-platform@25abaac + 本轮修改。

## 当前部署

TX 已运行 Jaeger 2.22.0：容器 `agent-ops-tracing-jaeger-1`，镜像固定于 compose.tracing.yaml 中的 SHA256。Badger 数据写入独立持久卷 `agent-ops-tracing-data`，保留 72 小时。Docker 开机启动已启用，容器 restart=unless-stopped；重启后四条验收 trace 的 span 数均保留。

| 用途 | 服务器地址 |
|---|---|
| UI/查询 | http://127.0.0.1:16686 |
| OTLP HTTP | http://127.0.0.1:4318/v1/traces |
| 健康检查 | http://127.0.0.1:13133/status |

端口只绑定服务器回环地址。在自己的电脑运行：

```bash
ssh -N -L 16686:127.0.0.1:16686 tx
```

保持隧道运行，浏览器打开 http://127.0.0.1:16686，选择服务 `agent-ops-platform`，或直接搜索 trace_id。

## 启动和维护

从仓库目录执行，要求 Docker 和 Compose：

```bash
bash scripts/start-tracing.sh
curl --noproxy '*' http://127.0.0.1:13133/status
docker compose -f compose.tracing.yaml logs --tail 50 jaeger
docker compose -f compose.tracing.yaml restart jaeger
```

启动脚本创建专用卷，并用 pgvector 镜像的一次性 shell 设置卷根目录 UID 10001；不会启动 PostgreSQL 或修改已有数据库卷。首次运行可能下载该辅助镜像和 Jaeger 镜像。

暂停使用 `docker compose -f compose.tracing.yaml stop`；重新启动运行启动脚本。外部卷独立于容器，移除它会删除 trace 数据，日常维护无需删除卷。容器限制 768 MiB/1 CPU，日志轮转 10 MiB × 3；单次闲置快照约 12.53 MiB，未做容量压测。

## 应用导出配置

```bash
export AGENT_OPS_TRACE_EXPORTER=otlp
export AGENT_OPS_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
export AGENT_OPS_TRACE_SAMPLE_RATIO=1
uv run python -m agent_ops
# 或启动应用 API
uv run uvicorn agent_ops.api:create_app --factory --host 127.0.0.1 --port 8000
```

默认 exporter=none；console 可用于本地检查。采样比例取值 0..1，默认 1，采用 ParentBased 比例采样。TX 的私有 `.local/ark.env` 也已启用 OTLP，启动前需要显式 source；该文件不自动加载、不提交、不打包。

OTLP HTTP 导出超时 3 秒，批量队列上限 2048、批次 256、调度间隔 1 秒。本机 collector 连接不使用环境代理。API/CLI 关闭自己创建的 Dispatcher 时等待导出排空；外部注入的 Dispatcher/Tracer 由调用者关闭。关闭和过载情况下的导出不等于无损审计日志。

## 实测与边界

```bash
uv run python scripts/verify-tracing-backend.py
```

验证脚本使用确定性 planner，无付费模型调用，真实通过 OTLP 导出并查询 `/api/v3/traces/{trace_id}`。2026-10-08 成功/422/依赖阻塞/主动取消四类分别查到 10/1/9/4 个 span，检查父子关系、blocked_by、状态及正文不泄漏。安全结果见 [验收记录](measurements/2026-10-08-tracing-backend.json)，原始查询仅保留在被忽略的 .local 目录。

这是单机开发追踪后端；业务 API 尚未作为常驻服务部署，没有跨服务 traceparent、HA、告警或压力容量验收。72 小时后 trace 正常过期；已有测试用内存 exporter 继续用于离线测试。

参考：[Jaeger 官方发布](https://github.com/jaegertracing/jaeger/releases/tag/v2.22.0)、[Badger 持久化](https://www.jaegertracing.io/docs/2.21/storage/badger/)。
