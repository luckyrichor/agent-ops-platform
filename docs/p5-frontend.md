# P5 前端实施与接口约定

来源：2026-10-27 用户 P4 验收及 P5 授权；重读 B3/B4/B5。P4 已提交推送 1b344abfc78a62f48ed226df297ccc5f92ff4856；本轮仅 P5，P6 前停止。

React + TypeScript：同一代码包提供 /customer 客户网页与 /workbench 人工工作台。清晰的导航、状态与错误提示优先，不依赖外部图片。商品浏览调用真实 PG 商品接口、下单和订单调用既有后端；新增 GET /products、/{id} 只是暴露已有数据层能力，不做 P7 语义检索。导购可验证现有 SSE 编排演示，显式标“固定工具演示 / 真实商品检索尚未开通”，不把演示推荐连接到真实下单。支付页仅显示“尚未开通”，不发送支付或退款请求。

凭据只在单个标签页 JavaScript 内存，刷新页面需重新登录；不使用 localStorage/sessionStorage、URL、跨标签广播保存/同步令牌。API 客户端串行轮换刷新令牌、重试沿用幂等键，退出清空会话和业务状态。截图统一遮挡 data-private 邮箱/密码输入，不启用含网络凭据的 Playwright trace/video，不打印请求头或账号密码。

同源 /api 代理 HTTP/WS 到 127.0.0.1:8003，前端绑定 127.0.0.1:5173，不开公网。WS after 在当前会话内记录最后已处理序号，处理成功后再推进；重连复用，退出归零。工作台只在有业务变更时刷新，忽略仅 lease_renewed/order_viewed 的推送回声，避免“读详情续租→推送→读详情”的反馈循环。为此给事件增加 change 字段，不改既有 kind/sequence。

Playwright 用一次性真实 PostgreSQL + 两个浏览器身份，覆盖客服完整主流程、实时回复、断线重连、同一浏览器不同标签页账号隔离、真实商品下单/订单隔离和支付/检索未开通。私有随机演示凭据和截图只在 .local；不写入代码或日志。后端四门、前端类型检查/构建/Playwright 后汇报证据。

## 本机运行与 SSH 访问

后端与前端都在服务器 `tx` 上运行。不要把 host 改成 `0.0.0.0`，不要开放云防火墙端口。

服务器终端一（前台运行后端；P2 有已配置的演示账号和业务数据）：

```bash
ssh tx
cd /home/ubuntu/project/WorkPlan/agent-ops-platform
uv sync
AGENT_OPS_PLANNER=deterministic uv run python scripts/serve.py \
  --backend postgres --env-file .local/p2-application.env --port 8003
```

服务器终端二（前台运行生产构建预览；代理只连 127.0.0.1:8003）：

```bash
ssh tx
cd /home/ubuntu/project/WorkPlan/agent-ops-platform/frontend
npm ci
npm run build
npx vite preview --host 127.0.0.1 --port 5173 --strictPort
```

用户电脑终端（保持窗口开启）：

```bash
ssh -N -L 127.0.0.1:5173:127.0.0.1:5173 tx
```

浏览器访问 `http://127.0.0.1:5173/customer` 或 `http://127.0.0.1:5173/workbench`。前端代理同时转发 HTTP 和 WS，不需要另外暴露/转发 8003。若用户电脑 5173 已占用：

```bash
ssh -N -L 127.0.0.1:15173:127.0.0.1:5173 tx
```

改用浏览器 `http://127.0.0.1:15173/customer`。退出时 Ctrl+C 停止两个服务器进程及本地转发。用户电脑到 tx 的 SSH 实连未代用户验证；服务器回环 HTTP/WS 已通过 Playwright。演示账号名单在服务器 `.local/demo-accounts.json`（600），不要贴进群或前端源文件，登录后截图也不展示账号邮箱。

不开网页先查看截图，可在用户电脑执行（Windows 将目的路径替换成自己的下载目录）：

```bash
scp -r tx:/home/ubuntu/project/WorkPlan/agent-ops-platform/.local/p5-2026-10-27/final/screenshots \
  ~/Downloads/agent-ops-p5-screenshots
```

## 实际前后端契约

- 客户 `/customer`：登录、导购/客服同一窗口、商品浏览与详情、真实下单、我的订单与物流、退货转人工、支付骨架。工作台 `/workbench`：待领取/我领取的、OFFSET 分页、详情/订单/路由理由/操作记录、领取/回复/解决/退回；管理员额外看服务概览。
- 共享一个 React 构建和 API 客户端，两个角色入口分别渲染；不是两个独立部署项目。原后端权限仍是最终边界，前端角色判断只控制入口与按钮。
- GET /products?limit&cursor、GET /products/{id} 新增公开只读接口，调用 P2 已有真实 PG 商品能力，商品分页沿用既有 after；不引入工单 S1 游标优化，也不做语义/自然语言检索。无数据库/不可用商品会显示错误或空态，不用前端假商品替代。
- POST /orders 使用真实 product_id/quantity，成功后展示真实 order_id/status。支付页不请求支付接口，没有二维码、沙箱跳转或伪造成功；退货只进入既有 P4 人工流程。
- POST /v1/commerce/stream 用现有 SSE 传 started/heartbeat/result 状态。本期仅展示明确标识的固定工具编排演示，不展示演示目录商品为真实推荐、不将其接入下单；这不是模型逐 token 生成，也不是 P7 真实商品/记忆接入。
- GET /workbench/tickets 新增可选 state=claimed&mine=true，用现有领取者字段恢复自己的工单；默认仍 waiting_human、OFFSET。没有新增索引、领取算法或 S1 测量。
- push payload.change 区分 lease_renewed/order_viewed 与业务事件。前端忽略这两类读回声，其他业务事件成功处理后再推进 after；重连继续补发。工作台详情读取与业务写串行，使用返回 ticket_revision，外部版本冲突时刷新并保留草稿，不静默覆盖。
- 同一标签页串行刷新令牌，401 重试保留幂等键；网络失败重试同一业务载荷复用 key，成功后释放 key。退出卸载页面/WS、清空状态和令牌。无跨标签登录广播，无本地持久化令牌；刷新网页需要重新登录是本期明确选择。
- HTTP/WS 都通过当前来源 /api 代理，平台令牌只发平台 API，不发 agent-memory 或外部服务。API target 配置只允许 127.0.0.1；源码、构建、日志与截图不包含实际密码/签名密钥/令牌。

## 验证与截图口径

```bash
cd /home/ubuntu/project/WorkPlan/agent-ops-platform
AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q
uv run ruff check src tests
uv run mypy --strict src
uv run python -m agent_ops.evals.support_runner
npm run --prefix frontend typecheck
npm run --prefix frontend build
uv run python scripts/verify-p5.py --output .local/p5-new-run \
  --browser /home/ubuntu/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome
```

`verify-p5.py` 启动一次性真实 PG、迁移/随机私有账号、真实 API、生产构建预览和 Playwright；测试完成全部停止并清理测试容器，不改 P0/P2。输出路径必须新建。依赖浏览器可先在 frontend 跑 `npx playwright install chromium`；本轮下载未继续推进而停止，实际使用服务器已安装的 Chrome for Testing 153.0.8010.12，Playwright 1.64.1。`--browser` 可指定已有兼容浏览器，省略时用 Playwright 配套浏览器。

最终 5 项浏览器用例覆盖：真实客服完整主流程/WS 重连/操作记录；同一浏览器两个标签页不同顾客隔离（含跨用户订单 404、退出不影响另一标签）；真实 PG 商品浏览下单/库存预留/订单详情/未开通支付/SSE 演示；退回重领/我的领取/管理员统计；移动端无横向溢出。固定测试商品来自一次性库夹具，截图展示真实 UI 和真实数据库接口，不冒称 P2 全量数据截图或 P7 真实商品检索效果。

最终截图在 `.local/p5-2026-10-27/final/screenshots/`，15 张，完整路径/文件哈希见本轮报告。统一遮挡邮箱、密码及 data-private 字段；trace/video/自动失败截图关闭，测试日志对随机凭据过滤。截图已抽查。仅验证 Chromium 桌面/移动视口；Firefox、Safari、真实手机、用户电脑 SSH 实连与百万库浏览器写入联调均未验证。
