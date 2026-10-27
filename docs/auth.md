# P3 认证与权限

最后更新：2026-10-26。设计取舍见 [design-decisions.md](design-decisions.md) 的 P3 节。

## 登录

`POST /auth/login` {email, password} → {access_token, token_type: "Bearer", expires_in: 900, refresh_token, refresh_expires_at, account}。之后请求带 `Authorization: Bearer <access_token>`。访问令牌 15 分钟；`POST /auth/refresh` {refresh_token} 换一对新令牌（旧刷新令牌作废，重放会吊销整个登录）；刷新令牌 7 天，同一次登录最长 30 天。`POST /auth/logout` 结束当前登录；`GET /auth/me` 返回账号信息。

## 接口与角色

| 接口 | customer | agent | admin | 说明 |
| --- | --- | --- | --- | --- |
| POST /auth/login、/auth/refresh | 公开 | 公开 | 公开 | |
| POST /auth/logout、GET /auth/me | ✓ | ✓ | ✓ | |
| POST /support/requests | ✓ | 403 | 403 | 工单归属当前顾客；Idempotency-Key 按顾客隔离 |
| GET /support/tickets | 自己的 | 403 | 403 | 游标分页，新的在前 |
| GET /support/tickets/{id} | 仅自己的，否则 404 | 全部 | 全部 | |
| GET /support/conversations、/{id}/messages | 仅自己的，否则 404 | 403 | 403 | 读 P2 的 commerce 会话表 |
| POST /workbench/tickets/{id}/resolve | 403 | ✓ | ✓ | 审计事件 actor=`account:<id>` |
| POST /support/tickets/{id}/resolve | 403 | ✓ | ✓ | 旧路径兼容别名，与上一行相同 |
| GET /orders、/orders/{id}；POST /orders、/orders/{id}/cancel | 仅自己的，否则 404 | 403 | 403 | 下单的 user_id 取自令牌，请求体带 user_id 返回 422 |
| GET /admin/accounts；POST /admin/accounts/{id}/status | 403 | 403 | ✓ | 停用账号会吊销其刷新令牌；不能停用自己 |
| POST /v1/commerce/runs、/stream | ✓ | 403 | 403 | SSE 响应开始前鉴权；记忆暂不接入 |

未登录或令牌无效返回 401 + `WWW-Authenticate: Bearer`，detail 为 AUTH_REQUIRED / ACCESS_TOKEN_EXPIRED / ACCESS_TOKEN_INVALID / INVALID_CREDENTIALS / REFRESH_TOKEN_INVALID / REFRESH_TOKEN_REUSED；角色不符返回 403 ROLE_FORBIDDEN。memory 模式下 /orders 和会话接口过了鉴权后返回 503 COMMERCE_BACKEND_UNAVAILABLE。`tests/test_auth.py` 用 OpenAPI 路由表做断言：新增接口如果不在角色矩阵里，测试就失败。

## 与 P3 之前的兼容性变化

- `POST /support/requests`：原来匿名可用，现在要 customer 登录。响应多一个 customer_user_id 字段。
- `GET /support/tickets/{id}`：原来匿名可读，现在顾客只能读自己的，客服/管理员能读全部。P0 历史测试工单保持无归属；P2 仅按确定的关联会话补归属，其余无归属工单只有客服/管理员能读。
- `POST /support/tickets/{id}/resolve`：原来用共享 `SUPPORT_HUMAN_TOKEN`（缺失时 403），现在用 agent/admin 访问令牌（缺失时 401，角色不符 403）。SUPPORT_HUMAN_TOKEN 不再读取。
- 服务层签名：`SupportService(tracer)`、`PostgresSupportService(url, tracer)` 去掉 human_token 参数；`resolve(..., actor)` 第三个参数从令牌改为审计用的操作者。
- postgres 模式新增必填环境变量 `AGENT_OPS_AUTH_SECRET`（不少于 32 字节）；`scripts/stack.py` 会生成到 .local。
- 当前 benchmark-b0/b1 默认先登录，计时之外建账号和登录；显式 `--auth off` 走仅基准使用的认证关闭工厂。历史标签及旧结果保留，在旧标签可按原脚本精确复现。旧 verify-p1-http 仍限历史标签；当前 TCP 验证使用 verify-p3-http。

## 演示账号

```bash
uv run python -m agent_ops.demo_accounts --init   # 在 .local/demo-accounts.json 生成 3 顾客/2 客服/1 管理员，随机密码，权限 0600，不打印
uv run python -m agent_ops.demo_accounts          # 按名单建账号；数据库 URL 取 AGENT_OPS_DATABASE_URL 或 .local/application.env
```

名单也可以用环境变量 `AGENT_OPS_DEMO_ACCOUNTS`（JSON）传入。顾客没写 customer_user_id 时，按 id 从小到大选有订单、且尚未关联账号的 commerce.users。重复执行不会重复建账号；`--reset-passwords` 重新哈希密码并结束已有登录。输出只有邮箱、角色、关联 id 和动作。目标库需要先升级到迁移 `0004_auth`。

真实 TCP 联调（一次性 PG 容器 + 两个 uvicorn 进程，不碰服务器库）：`uv run python scripts/verify-p3-http.py --output .local/p3-http/result.json`。

## 未覆盖

注册接口；登录失败限流（P12）；/ws；工作台队列、抢单、回复（P4）；管理员统计（P4）；客服按工单查看关联订单（P4）；前端令牌存放方式（P5）。support 的“查订单”路由仍用 demo-100/demo-200 固定夹具，不校验订单归属，由 P4 改读真实订单表并按归属过滤。

## P3 导购与记忆边界（P7 前缺口）

已核对安装 SDK 固定提交 `1e2730c0a0b6a25f2ee9f102dadcd3ca8288a474` 和相邻仓库提交 `2fae08bdb524497070b6cd8ab5cdbaabfff5ced1` 的 `src/agent_memory/api/auth.py`：均只由 RS256 JWT 的 tenant_id/sub 提取 UUID，未提供服务凭据加平台 bigint 用户 ID 的协议。平台 HS256 访问令牌的受众和签发方属于平台，禁止发给其他服务，也不自行把 bigint 填进 UUID 或用共享用户规避隔离。

因此 `/v1/commerce` 登录后只运行编排，忽略 AGENT_MEMORY_URL，不构造 memory HTTP/SDK 客户端；`memory_status`、`memory_read_status`、`memory_write_status` 均为 `not_configured`。即使提交 memory_id 也不会读取记忆或回显为已关联记忆。底层 MemoryCommerce 和独立 SDK 测试保留，不能称当前平台 HTTP 已打通记忆。P7 前须讨论服务身份与逐顾客隔离约定；如需改 agent-memory API，先向负责人报告，批准后同步双方文档。

客服直接 GET /orders 和 /orders/{id} 在 P3 仍为 403；P4 才能通过工单详情查看本工单关联订单，每次记录审计事件，不增加任意用户订单查询权限。

## 基准运行

当前脚本：`uv run python scripts/benchmark-b0.py --output .local/bench-auth-new`；PostgreSQL：`uv run python scripts/benchmark-b1.py --output .local/bench-pg-auth-new`。均默认 `--auth on`，显式 `--auth off` 关闭基准认证。正常开发入口 `agent_ops.api:create_app` 不读取该开关。基准工厂 `scripts/benchmark_app.py` 仅由基准脚本在 loopback 启动；不得用其作为业务入口。

账号建立和一次登录在 warmup 前、所有计时和 CPU 快照之外。每轮客户端复用同一有效令牌，签名检查算入每次请求成本；本轮固定小测不跨 15 分钟有效期。凭据临时文件权限 600、只在 .local，结束清理。输出记录 enabled、login_in_timing=false、独立标签和源码摘要。P3-authenticated 与 P3-auth-disabled-compatibility 都不是历史 B0/B1；异日期数字不直接比较。

## 本机 P2 演示

两个库均已升级，P2 的 6 个演示账号名单只在服务器 `.local/demo-accounts.json`（600）。主库仍保持 0 个业务用户/订单，不在主库创建演示顾客。

```bash
uv run python scripts/serve.py --backend postgres --env-file .local/p2-application.env --port 8003
```

此命令显式使用 P2 验收库，绑定 127.0.0.1；默认 `.local/application.env` 仍指向 P0 主库。密码从私有名单本机读取，不发送到群、仓库或日志。`scripts/verify-p3-persistent.py` 已用同一配置起两个临时 TCP 进程验证，结束后停止，不增加常驻 API。
