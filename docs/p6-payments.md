# P6 支付沙箱：接口、入口与验证边界

2026-10-28，Codex。本阶段只做 P6；S2 的正确性保护随支付实现，P10 的并发性能实验尚未执行。

## 实际实现

`0006_payments` 在独立 payments schema 中保存支付、通知摘要、退款和审计。支付与退款金额均为整数分，来自自己的订单表；客户端不能指定支付金额。每订单一个稳定商户订单号，每全额退款一个稳定请求号。订单、库存、支付状态、通知去重与审计在数据库事务中修改。所有金融路径先锁订单，再锁支付，退款路径随后锁退款和工单。

通知只用私有配置中的支付宝公钥验 RSA2，再核对应用、商户、自己的订单号/金额以及已绑定的平台交易号。通知编号唯一且摘要冲突拒绝。重复通知不会再次扣库存或入账；已支付状态不会被等待付款/关闭的迟到通知回退；取消后的迟到支付进入人工核查，不伪装入账成功。通知事务提交后才返回纯文本 success；验签/业务校验失败返回 failure 并记录固定原因码。历史探针无条件 success 不属于此实现。

生成支付链接后，客户本地取消被拒绝，以免仍可支付的链接对应库存已释放。未生成链接的超时订单可以本地取消；已提交订单须主动查单并在必要时向网关关单。查询不明确时保留库存与待确认状态。超时阈值当前为 30 分钟，不是库存容量实验结论。

退款只允许领取退货工单、租约有效且版本匹配的座席批准，关联订单必须已支付且属于工单顾客。当前只支持整单全额退款。接口成功或超时都复用同一退款编号；不确定结果先查询，只有经公钥验签且订单、平台交易号、金额匹配的退款成功结果才更新本地退款、订单与工单。查询不明确保持 pending_confirmation。自动补偿目前依赖主业务 API 生命周期，独立通知处理器只负责入账；单独运行入口不会自动补偿。

## 客户与座席接口

| 接口 | 身份与行为 |
|---|---|
| GET /payments/availability | 顾客；是否已配置支付 |
| GET /workbench/payments/availability | 座席；是否已配置支付 |
| POST /orders/{order_id}/payment | 顾客，只能支付自己的待付订单；Idempotency-Key 必填，业务幂等边界是订单唯一支付记录 |
| POST /workbench/tickets/{ticket_id}/refund | 座席，expected_revision 与 Idempotency-Key 必填；已批准的同工单/同座席请求返回同一退款记录 |
| POST /payments/notify | 仅独立通知应用，表单、RSA2、公钥与业务校验，不使用顾客令牌 |

未配置支付时，创建/退款返回 503，前端保持“尚未开通”。配置有效时页面明确标为沙箱，链接只保留在标签页内存，跳转不代表支付成功。外站请求不携带平台 Authorization；不保存签名链接到浏览器持久化存储。已有订单详情是支付查询入口，不额外开放公共查单接口。客户网页/API 继续回环访问，SSH 方法沿用 `p5-frontend.md`。

## 私有文件与出站

`.local/alipay/` 目录 700，`app_id.txt`、`seller_id.txt`、`gateway_url.txt`、`app_private_key.txt`、`alipay_public_key.txt`、`notify_url.txt` 文件 600。支持页面复制的 DER Base64 或 PEM，私钥与公钥须为 RSA 至少 2048 位；不改原始文件，不打印内容。网关仅从给定文件读取，不内置实际端点。配置文件不允许符号链接。

`AGENT_OPS_PAYMENT_DIR` 启用支付；`AGENT_OPS_PAYMENT_TIMEOUT` 默认 5 秒（上限 30）；`AGENT_OPS_PAYMENT_RETRIES` 默认 2（上限 3），即最多 3 次出站尝试。连接/读取失败及 5xx 使用有界退避；4xx、业务拒绝、验签失败不盲目重试。TLS 验证开启，不跟随重定向；网关响应最多 1 MiB。真实响应存在 GB18030 编码，按原编码的原始 JSON 子串验签，禁止重新序列化后验签。

只读检查确认网关出站经过已有 Mihomo TUN 规则分类，当次探测命中 DIRECT。`trust_env=False` 避开显式代理环境变量，不能绕过 TUN；未来 Mihomo 路由变化或不可用仍可能影响网关调用。本阶段没有修改其配置。

## 已批准的独立入口

独立 Nginx 监听 18080，转发回环 18081 的最小通知应用；不改现有 Nginx，不暴露主 API 或前端。仅精确 `/payments/notify` 的 POST，其他路径 404、其他方法 405，64 KiB 请求体，连接/请求超时有界。转发时清除 Authorization、Cookie 与转发来源头。

`--rate`（默认 5 次/秒/来源）、`--burst`（20）、`--connections`（10）可配置；这些值**未做容量验证**。限流返回 429，后续依靠提供方重试和主动查询补偿。访问日志仅时间、固定路径、方法、状态、响应字节数、耗时、内部关联号；不含地址、查询串、头、表单、签名、账号或凭据。未知路径不记录，错误日志关闭以免 Nginx 错误文本带入客户地址。日志轮转每日或达到大小阈值，保留 7 份，私有权限。

先确认云端端口允许规则再启动（运行参数是操作者确认标记，不会修改云规则）：

```bash
uv run python scripts/payment-entry.py --env-file .local/payment-application.env
uv run python scripts/payment-entry.py --env-file .local/payment-application.env --start --public-confirmed
```

可安装用户级守护与日志轮转定时器：

```bash
uv run python scripts/payment-service.py --env-file .local/payment-application.env
# 已确认开放且已准备持久数据库后才激活：
uv run python scripts/payment-service.py --env-file .local/payment-application.env --activate --public-confirmed
systemctl --user status agent-ops-payment-entry
systemctl --user stop agent-ops-payment-entry
systemctl --user disable agent-ops-payment-entry agent-ops-payment-logrotate.timer
```

用户服务安装器只修改独立用户 unit；不配置防火墙、不替换系统 Nginx。持久库升级需先备份；不要把一次性联调库作为长期运行库。含已入账或退款记录时 downgrade 主动拒绝，避免抹掉财务证据；空金融库的迁移往返仍可执行。

## 对账与未完成的线上验收

```bash
uv run python scripts/reconcile-payments.py --env-file .local/payment-application.env --download-date YYYY-MM-DD --output .local/reconciliation/result.json
# 或用户从平台导出的原始账单：
uv run python scripts/reconcile-payments.py --env-file .local/payment-application.env --bill .local/reconciliation/bill.csv --bill-date YYYY-MM-DD --output .local/reconciliation/result.json
```

账单下载地址必须出自验签后的网关响应，并限制 HTTPS、提供方域名与大小，不跟随跳转。ZIP 不落盘解压；金额、缺失、额外记录和重复行均产生差异。文件规范化支持明确列名；退款缺少请求编号、未知格式或下载失败都报不可用，不能输出“差异为零”。日期按北京时间界定本地账本，未提供同范围外部账单不能声称完成对账。

真实沙箱付款、正式回调、退款、平台账单零差异及长期服务上线的结果，见本阶段进展记录；尚无证据的项目必须标为未验证。模拟签名/模拟传输测试只验证正确性与失败边界，不替代真实平台验收。

支付宝官方退款查询约定：查询操作成功并不等于退款成功，应检查 REFUND_SUCCESS，未知时保持待确认：[官方接口说明](https://aipay.alipay.com/docs/vibe-pay/ai-web-app-payment-qianyi/api-list/alipay-trade-fastpay-refund-query.html)。
