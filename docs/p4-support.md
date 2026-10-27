# P4 客服闭环实现约定

来源：本轮负责人 P3 验收及 P4 授权，实际执行日期北京时间 2026-10-27；重读方案第六节、附录 B2/B3/B4 与 S1。本轮只做后端，P5 页面和 P6 支付退款不实施。

- 产品 PostgreSQL 客服适配器采用四状态。创建版本 1；后续转换与消息、关联订单查看、SLA 标记都递增版本并追加审计。claimed 只能由领取者操作；初始租约 10 分钟，合法座席操作自动续期。过期必须释放回 waiting_human 后再领取，不允许旧持有者续活过期租约。
- 查询物流读 commerce.orders/shipments 且按登录顾客过滤。缺订单的物流进入 needs_information，顾客补充后重新判断并 resolved；退货始终只进入 waiting_human，缺订单可在等待时补充关联信息，保持 waiting_human。退货不调用退款；resolved 终态。客服只能 GET /workbench/tickets/{id}/order 查看该工单关联且属于其顾客的订单，每次查看追加审计。直接 /orders 仍拒绝 staff。
- 队列按优先级降序/创建时间升序/工单 ID 稳定排序，用 limit/offset。产品指定工单领取是一条 UPDATE WHERE state/revision RETURNING，事务内追加审计/推送，不先查再写。两步领取只在独立实验脚本的显式危险开关下提供，不挂产品路由。不新增队列部分索引/游标/SKIP LOCKED，不运行 S1。
- 新客服消息存 support.messages，sender_role 为 customer/agent/system，正文不进审计、日志或 trace。既有 commerce.messages 不更改，ECD sender=source 保持原样；详情消息列表同时返回 origin，避免冒称种子说话人。
- 全局推送日志 support.push_events 持久化，使用事务内单行计数器分配序号，计数器锁持有至提交，避免自增序列的事务提交乱序造成断线补发漏消息。WebSocket 第一帧认证，不在 URL 放 JWT；after 指最后已处理序号，返回严格大于它的可见事件。角色与顾客过滤在服务端执行；多进程读取同一持久日志，轮询只影响通知延迟，不影响可恢复性。将来可用 Redis Pub/Sub 通知唤醒，仍以 PG 日志补发，不能依赖 Pub/Sub 保存历史。客户端持久化最后处理序号并按序号去重，网络中断时不声称传输层 exactly-once。
- 生命周期任务每秒检查租约和 SLA，多个进程使用条件更新避免重复。默认 urgent/normal/low 截止时间分别 1/24/48 小时，提前 5 分钟预警，逾期记录标记。初始值可在后续产品决策时调整；测试使用受控数据库时钟/时间字段，不用真实等 10 分钟。
- /workbench 队列、详情、消息、领取/回复/退回/完成；/support 补充信息/顾客消息/退货；/admin/support-stats；/ws。新写接口要求 Idempotency-Key，重复返回原快照，不重复事件；同 key 不同载荷 409。所有正常 HTTP 响应沿用 trace 中间件。

验收：真实 PG 并发领取唯一、版本冲突/越权、幂等与事务回滚、租约续期/超时回收、SLA 预警/到期、种子角色保留、工单内订单查看每次审计、真实 TCP WebSocket 收到创建→领取→回复→解决及断线补发，多进程消费者和重启后日志可读；完整四道门。不得把未运行的实库迁移、性能实验或 P5 前端写成已完成。

## 接口与调用顺序

顾客/客服/管理员继续用 P3 `/auth/login` 的访问令牌。工单字段新增 `order_id`、`assigned_account_id`、`lease_expires_at`、`priority`、`sla_due_at`、`sla_warned`、`sla_overdue`。新写操作都要求 `Idempotency-Key`；`expected_revision` 必须取最新返回版本，同 key 同载荷返回原快照，同 key 不同载荷 409。

| 接口 | 身份 | 内容 |
| --- | --- | --- |
| POST /support/requests | customer | 路由问题；物流读真实归属订单，退货/人工入 waiting_human |
| POST /support/returns | customer | 显式退货申请，永不直接退款 |
| POST /support/tickets/{id}/information | customer，自己的 | 补缺失订单，校验真实归属；needs_information 重新判断，waiting_human/claimed 保持原状态；已有关联不可改成另一单 |
| POST /support/tickets/{id}/messages | customer，自己的 | expected_revision + message；终态拒绝追加 |
| GET /support/tickets | customer | 产品 PG 模式 limit/offset，含完整 P4 工单字段；显式 cursor 返回 P4_USES_OFFSET |
| GET /support/tickets/{id}/messages | customer，自己的 | limit/offset；含 origin=seed/live 和真实 sender_role |
| GET /workbench/tickets | agent/admin | 待处理队列，priority DESC、created_at、ticket_id；limit/offset |
| GET /workbench/tickets/{id} | agent/admin | 工单、前 50 条消息和审计；已领取者查看自动续租并审计 |
| GET /workbench/tickets/{id}/messages、/events | agent/admin | 分页消息/审计；持有者读操作也续租，返回 ticket_revision |
| GET /workbench/tickets/{id}/order | agent/admin | 只读当前工单已关联且归属一致的真实订单，含商品行/物流；逐次追加 order_viewed 审计，返回 ticket_revision |
| POST /workbench/tickets/{id}/claim | agent/admin | waiting_human + expected_revision 的单条 CAS；租约 10 分钟 |
| POST /workbench/tickets/{id}/reply | 领取者 | expected_revision + message，续租、审计、消息推送 |
| POST /workbench/tickets/{id}/release、/resolve | 领取者 | 退回/完成，版本递增，清空领取者与租约；过期持有者拒绝 |
| POST /support/tickets/{id}/resolve | 领取者 | 旧路径别名，产品 PG 模式同样必须先领取、带幂等键 |
| GET /admin/support-stats | admin | 队列长度、当前等待工单年龄均值、SLA 到期数、各客服解决量 |
| /ws | 已登录 | 首帧 `{access_token, after}`；服务端按角色/顾客过滤，后续可发纯文字 ping 获 pong |

访问别人的工单/会话/订单均不泄漏内容；staff 直接 /orders 保持 403。B1 允许 staff 读取所有工单，因此可以读取任意可见工单的已关联订单，但不能换一个 order_id、列出该顾客其他订单或绕到任意订单接口。查看订单是负责人要求的“每次查看都审计”例外，GET 会递增工单版本；客户端必须使用返回的 ticket_revision 再写，避免假设旧版本仍有效。

顾客补充订单可发生在领取之后；此时保持 claimed、领取者和原租约，顾客操作不会续座席租约。只有 needs_information 的物流补充能自动 resolved。客服关闭退货咨询不等于批准或执行资金退款；P4 不提供退款批准/退款调用接口。

## 消息与推送一致性

新消息只进入 support.messages，不重写 commerce.messages。原种子 sender=source 保持 source；不把交替话轮或文本猜测转为 customer/agent。消息正文只在受保护消息接口读取，不写入 span、审计 payload 或 WebSocket 事件；事件仅带消息 ID/角色、状态/版本/领取者/租约等结构信息。

状态更新、消息、幂等快照、审计和 push_events 同一事务提交。push_clock 是事务性提交顺序门，事务回滚不消费可见序号；避免 PostgreSQL sequence 提前分配但提交乱序时，客户端跳过晚提交的小序号。可见事件的序号允许因顾客过滤而有间隔，要求严格递增，不要求每个客户端看到全局每个序号。

断线后带“最后已处理”序号，服务端只发严格更大的可见事件。客户端须持久化最后处理序号并去重；只收到但未处理完成的事件应重放，不能把网络 ACK 当作应用已消费。这是恢复协议，不声称网络层 exactly-once。P4 不清理历史推送日志，保留完整重放能力；保留期限与游标过期协议以后单独设计。

当前两个 OS 进程直接轮询共享 PG 日志，已测在另一个进程上重连补发。未来 Redis Pub/Sub 只用于唤醒，丢通知仍从 PG 按 after 补发。单行提交顺序门有写入串行开销、轮询有延迟和数据库负担；吞吐/百万级推送性能未验证，不作 S1/S6 优化结论。

## 运行、迁移与边界

- 运行环境 Python 3.12；新增 websockets，实际锁定版本见 uv.lock。`uv sync` 后 `uv run python scripts/serve.py --backend postgres --env-file .local/p2-application.env --port 8003`，仅 loopback，按需运行，不常驻。
- 两库从 0004 迁移到 0005，先做 .local/backups 自定义格式备份，拒绝在有其他连接时维护，逐库恢复准入。脚本 `scripts/apply-p4-databases.py`，已有报告拒绝重复。新字段给历史工单初始化 normal/迁移时刻 +24h 的 SLA；这是合成演示的新起点，不是追溯认定历史真实 SLA。未回填历史订单关联，NULL 关联查看订单返回 404，禁止猜测。
- 0005 downgrade 在仍有 claimed 工单时拒绝，必须先释放再降级。降级会移除 P4 专属消息/推送/幂等表及字段，必须先备份；测试库往返已验证，服务器未执行 downgrade，完整备份恢复演练未验证。
- Product 只在 PostgreSQL 适配器 WorkbenchService 上启用闭环；内存及显式 legacy_support 是保留的历史基准，P4 新接口在该模式返回 503。基准工厂显式选 legacy_support，不代表其两步领取可被产品调用。原 P3 订单/会话/管理列表游标和顾客归属索引保留，没有在 P4 把它们当作 S1 新优化。
- 生命周期默认每秒扫描一批，多个进程条件更新确保仅一个过期释放/预警/到期事件；SQL 失败会记录固定错误提示并重试，不打印连接 URL/秘密。工作台流量/大量历史到期工单处理速度未验证。

验证命令：`AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q`；`uv run ruff check src tests`；`uv run mypy --strict src`；`uv run python -m agent_ops.evals.support_runner`。真实库只读烟雾：`uv run python scripts/verify-p3-persistent.py --output-dir .local/p4-new-smoke --workbench`；创建认证会话，不新增业务订单或工单。固定规则评测不称真实路由准确率。
