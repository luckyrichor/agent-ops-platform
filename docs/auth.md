# P3 认证与权限

最后更新：2026-10-23。设计取舍见 [design-decisions.md](design-decisions.md) 的 P3 节。

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
| POST /v1/commerce/runs、/stream | 公开 | 公开 | 公开 | **未改**，等负责人决定（见下） |

未登录或令牌无效返回 401 + `WWW-Authenticate: Bearer`，detail 为 AUTH_REQUIRED / ACCESS_TOKEN_EXPIRED / ACCESS_TOKEN_INVALID / INVALID_CREDENTIALS / REFRESH_TOKEN_INVALID / REFRESH_TOKEN_REUSED；角色不符返回 403 ROLE_FORBIDDEN。memory 模式下 /orders 和会话接口过了鉴权后返回 503 COMMERCE_BACKEND_UNAVAILABLE。`tests/test_auth.py` 用 OpenAPI 路由表做断言：新增接口如果不在角色矩阵里，测试就失败。

## 与 P3 之前的兼容性变化

- `POST /support/requests`：原来匿名可用，现在要 customer 登录。响应多一个 customer_user_id 字段。
- `GET /support/tickets/{id}`：原来匿名可读，现在顾客只能读自己的，客服/管理员能读全部。P1/P2 遗留工单没有归属，只有客服/管理员能读。
- `POST /support/tickets/{id}/resolve`：原来用共享 `SUPPORT_HUMAN_TOKEN`（缺失时 403），现在用 agent/admin 访问令牌（缺失时 401，角色不符 403）。SUPPORT_HUMAN_TOKEN 不再读取。
- 服务层签名：`SupportService(tracer)`、`PostgresSupportService(url, tracer)` 去掉 human_token 参数；`resolve(..., actor)` 第三个参数从令牌改为审计用的操作者。
- postgres 模式新增必填环境变量 `AGENT_OPS_AUTH_SECRET`（不少于 32 字节）；`scripts/stack.py` 会生成到 .local。
- 依赖 B0/B1 匿名调用 /support 的历史脚本（benchmark-b0/b1、verify-p1-http）在当前 HEAD 上跑不通。这些脚本只在各自标签上复现，没有修改。

## 演示账号

```bash
uv run python -m agent_ops.demo_accounts --init   # 在 .local/demo-accounts.json 生成 3 顾客/2 客服/1 管理员，随机密码，权限 0600，不打印
uv run python -m agent_ops.demo_accounts          # 按名单建账号；数据库 URL 取 AGENT_OPS_DATABASE_URL 或 .local/application.env
```

名单也可以用环境变量 `AGENT_OPS_DEMO_ACCOUNTS`（JSON）传入。顾客没写 customer_user_id 时，按 id 从小到大选有订单、且尚未关联账号的 commerce.users。重复执行不会重复建账号；`--reset-passwords` 重新哈希密码并结束已有登录。输出只有邮箱、角色、关联 id 和动作。目标库需要先升级到迁移 `0004_auth`。

真实 TCP 联调（一次性 PG 容器 + 两个 uvicorn 进程，不碰服务器库）：`uv run python scripts/verify-p3-http.py --output .local/p3-http/result.json`。

## 未覆盖

注册接口；登录失败限流（P12）；/v1/commerce 登录身份；/ws；工作台队列、抢单、回复（P4）；管理员统计（P4）；客服按工单查看关联订单（P4）；前端令牌存放方式（P5）。support 的“查订单”路由仍用 demo-100/demo-200 固定夹具，不校验订单归属，由 P4 改读真实订单表并按归属过滤。
