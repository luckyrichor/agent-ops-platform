# W8维护 / W10 M4：调用树（Codex，2026-10-07）

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
`measurements/w10-call-tree.json`，包含成功与pricing失败两条调用树。
22个测试通过，含真实memory API/PostgreSQL旧回归、请求工具失败、memory降级、
并行取消收尾；不存在请求正文或异常正文导出。
来源agent-ops-platform@8c041be + 本轮工作树修改，源码SHA256见元数据。
