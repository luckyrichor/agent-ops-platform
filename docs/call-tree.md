# 调用树与终止语义（Codex，2026-10-08）

本次代码基线 agent-ops-platform@caa453f + 本轮修改；历史测量保留原始来源。

HTTP 调用树现在为 commerce.http → commerce.request → agent角色 → tool实际handler。blocked 任务有 agent 节点、outcome=blocked、task_id 和直接 blocked_by；没有实际执行就不生成 tool 节点。客户端断开/取消记录 cancelled，span 不标 ERROR；业务失败仍标 ERROR。

所有 HTTP 响应头含 X-Trace-Id，业务错误 JSON 保留 detail 并新增 trace_id；SSE started/error/aborted/result 也携带 trace_id。慢消费者恢复读取后获得 aborted/SLOW_CONSUMER，连接已断开时仅能依靠先前响应头及服务端 trace。

当前本地回归见 progress.md；新的演示证据为 measurements/2026-10-08-call-tree.json，含源码 SHA256、实际基线与 dirty 标记。下方 W10 证据及 22 个测试为历史验收，不表示当前版本。

## 历史：W8维护 / W10 M4（2026-10-07）

M4本地验收通过：commerce.request 根span → agent角色span → tool实际handler span；
可选memory get/search/remember HTTP工具也在同一根span下。
本地demo工具为目录/价格/邮费计算，不伪装成真实外部服务调用。
所有跨度使用OpenTelemetry当前context，asyncio并发保留父context。
响应返回trace_id，可据此找到完整请求；失败工具和Agent有ERROR状态与稳定原因码。

仅输出span名称、结果、memory状态、reason_code；不输出请求、返回正文、
凭证和异常事件。根span失败与工具失败不同：memory降级可见工具ERROR，
而导购仍成功。数据库内部span不属于本进程调用树，未配置跨服务traceparent传播。
未部署Jaeger/OTLP后端或常驻API；这是可运行、可导出的本地调用树。

W8维护：ready批次改为TaskGroup，使取消后等待所有并行角色finally完成，
测试同时取消pricing/shipping，不留下后台角色。

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy --strict src
uv run python scripts/call-tree-demo.py
AGENT_OPS_TRACE_EXPORTER=console uv run uvicorn 'agent_ops.api:create_app' --factory --host 127.0.0.1
```

控制台导出默认关闭；配置console后可检查span/parent/trace_id，示例JSON在
`measurements/w10-call-tree.json`（历史产物），包含成功与pricing失败两条调用树。
22个测试通过，含真实memory API/PostgreSQL旧回归、请求工具失败、memory降级、
并行取消收尾；不存在请求正文或异常正文导出。
来源agent-ops-platform@8c041be + 本轮工作树修改，源码SHA256见元数据。
