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

`--rate`（默认 5 次/秒/来源）、`--burst`（20）、`--connections`（10）可配置；这些值**未做容量验证**。限流返回 429，后续依靠提供方重试和主动查询补偿。访问日志仅时间、固定路径、方法、状态、响应字节数、耗时、内部关联号、请求字节数、正文类型分类、上游状态；不含地址、查询串、头、表单、签名、账号或凭据。未知路径不记录，错误日志关闭以免 Nginx 错误文本带入客户地址。日志轮转每日或达到大小阈值，保留 7 份，私有权限。

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


## 当前部署状态更新（2026-10-28）

独立持久沙箱库已从临时联调库的私有备份恢复，使用现有本地 PG 实例的独立数据库；P0/P2 原库仍是迁移 0005，无 payments schema。备份 `.local/p6-live-20261028/before-persistence.dump`，78,828 字节，600。私有 `.local/p6-live-20261028/application.env` 指向持久副本；原一次性容器已停止。

三个用户 unit 已 active/enabled：`agent-ops-p6-sandbox-api.service`（回环18884、含补偿）、`agent-ops-payment-entry.service`、`agent-ops-payment-logrotate.timer`。独立入口不依赖 bridge 会话进程；日志轮转手动执行成功。18884 仅供本轮沙箱联调，未切换用户既有前端代理。初始持久恢复遇到授权表名和目标依赖问题已修复，主库/P2 未删除或重建。

启动器遇到端口占用会拒绝启动，避免把旧监听误认成自身就绪；该拒绝路径已实测。新只读观测显示 Mihomo recovering，出现网关超时降级，未修改透明代理；本次状态不能沿用此前 DIRECT 的可用性结论。实际付款、退款、独立平台账单零差异仍待验证。此部署是持久沙箱，不是主业务库 P6 升级完成。


## 正式付款后的缺口（2026-10-29 补偿前历史）

实际签名查询返回交易成功且自己的订单/金额匹配，但不含 seller_id；查询结算字段也未补出商户身份。当前补偿实现严格要求该字段，因而仍不入账。异步通知中商户号校验保持必需。不能悄悄把缺失字段当成匹配；后续应取得完整平台通知或另行确认可验证的商户身份绑定方式。用户已确认沙箱交易详情没有通知重发功能，不应再把人工重发作为可执行的下一步；不需再次付款。

公开入口外部无效签名请求可达并拒绝400；这不是实际支付通知证据。请求499也不能识别其来源或证明业务处理。诊断新增仅请求长度、form/other正文类型分类和上游状态，不保存原始头/正文。当天账单下载尚不可用，额外请求HTTP404，实际退款和独立账单零差异未验证。


## 补偿身份缺口的处理边界（2026-10-29 人工绑定批准前历史）

查单的订单号不符仍为 `PAYMENT_QUERY_IDENTITY_MISMATCH`；订单号匹配但商户字段缺失或为空时，改为 `PAYMENT_QUERY_IDENTITY_UNAVAILABLE`，不与真实商户不匹配混为一谈。两种情况继续保持待确认，不扣库存、不关闭提供方交易、不发退款。收到合法异步通知后仍走原有严格校验和幂等事务，不受此前失败查询影响。

官方 SDK 的交易查询响应模型没有商户号字段；应用授权查询的响应虽有用户标识，其请求需要额外的 `app_auth_token`，现有私有配置不包含该授权令牌，不能假装接口已可用。依据：[交易查询响应模型](https://github.com/alipay/alipay-sdk-python-all/blob/master/alipay/aop/api/response/AlipayTradeQueryResponse.py)、[授权查询请求模型](https://github.com/alipay/alipay-sdk-python-all/blob/master/alipay/aop/api/domain/AlipayOpenAuthTokenAppQueryModel.py)、[授权查询响应模型](https://github.com/alipay/alipay-sdk-python-all/blob/master/alipay/aop/api/response/AlipayOpenAuthTokenAppQueryResponse.py)。这不等于断言所有官方接口都无法证明商户身份。

候选方案（尚未批准、未实现）：由用户在沙箱后台核对应用与商户的绑定，确认与私有配置一致后，建立仅保存在忽略目录中的人工绑定证明。查询补偿仍要求提供方公钥验签、自己的订单号/金额匹配；异步通知仍逐次核对商户号。该方案将部分商户证明来源改为人工核对，不能称为提供方签名的商户证明，须由用户明确接受这个取舍后才能实施，并在实际入账前确认绑定。另一选择是保留待确认，继续排查通知。未擅自增加绑定文件、跳过检查或强改财务状态。


## 当前结果：人工绑定补偿与真实退款（2026-10-29）

用户已在沙箱应用页核对应用绑定商户与私有卖家文件一致，核对日期为2026-10-29，并明确批准该信任前提。现在使用默认关闭的 `merchant_binding.json`：文件缺失时不启用补偿入账；有效文件必须600、非符号链接，含显式 verified=true、北京时间核对日期、应用和卖家标识的 SHA256 摘要。日期不得在未来；摘要与当前身份不匹配、格式错误均拒绝配置。实际标识及绑定文件只在忽略目录，不打印。

核对后记录配置：

```bash
uv run python scripts/confirm-merchant-binding.py --payment-dir .local/alipay --verified-on YYYY-MM-DD --confirm-manual-check
```

这个命令只记录操作者声明，不能代替后台人工核对，不查询或推断绑定关系。已有记录时拒绝覆盖；更换应用或卖家账号必须重新人工核对，先在私有目录归档旧记录，再生成新记录并重启服务。核对不能跨应用、卖家复用；公钥仍只使用 `alipay_public_key.txt`。

该配置仅在查单补偿的授权判断中使用：没有人工绑定记录，即使查询带商户号也不补偿入账；配置后仍要求原始响应公钥验签、自己的数据库订单号/金额匹配及成功交易状态才能入账。若查询含不同商户号仍拒绝；缺商户号的非成功态保留待确认。通知不使用人工绑定兜底，缺商户号返回 PAYMENT_MERCHANT_UNAVAILABLE，不匹配返回 PAYMENT_MERCHANT_MISMATCH，均拒绝入账。补偿入账在原事务内追加 PAYMENT_CAPTURED_MANUAL_BINDING，重复补偿不重复追加。

原已付款交易实际补偿5次：均confirmed，仅扣库存/预留各1，PAYMENT_CAPTURED与人工绑定审计各1。本地从pending转paid，无需再次付款。随后用原单走退货工单、座席领取/批准、退款，订单refunded、工单resolved，重复执行只有1笔退款/1条成功审计。独立退款查询通过公钥验签，返回REFUND_SUCCESS，订单号、交易号、退款请求号和金额均匹配。独立退款查询首次不可用，随后只读查询成功；没有额外新建退款请求。

实际有效异步通知仍为0；以上验证证明查单补偿与退款，不代表正式通知验收完成。外部当日账单下载两次均不可用，对账零差异未验证；限频容量未验证。主库/P2迁移仍未执行，现有前端代理未切换。真实通知重复投递、正式通知入账仍未验证；签名、金额、商户、重复/乱序的自动测试已覆盖，不能替代真实平台通知证据。未进入P7。

最终完整pytest209通过（2个既有JUnit警告、77.04秒），ruff与strict mypy27源文件通过；前端未改，Playwright本轮未重跑。公开脱敏证据 reports/p6-2026-10-29/merchant-binding.json，原始证据仅.local。


## 真实通知补验准备（2026-10-29，等待本轮开放确认）

用户要求新单通过真实异步通知入账。本轮已停止并暂时禁用上一轮的独立通知服务；没有修改云端规则，也不据之前的开放消息推断当前可达。独立Nginx配置预检通过。须先收到用户明确确认本轮云端入站TCP 18080已开放，再启用/启动 `agent-ops-payment-entry.service`，新订单和付款链接随后生成。该条件不是已经完成上线的证据。联调结束应删除本轮云端入站TCP 18080允许规则；是否实际删除以用户确认或云端只读证据为准。容量和限频值继续未验证。

### 通知留存与重放

私有环境变量 `AGENT_OPS_PAYMENT_NOTICE_ARCHIVE` 指向忽略目录下的700目录。本轮私有环境已准备 `.local/payment-notices`，入口尚未启动。正式处理器在通知公钥验签、订单/金额/商户核对并提交后归档；不合格请求不归档。记录是600 JSON：字段展示全部脱敏，精确签名字段值用AES-GCM加密，解密密钥单独600保存；不保留HTTP头、来源地址、Cookie或平台令牌。重放时仅在内存解密，不输出原始值。脱敏后替换字段值的副本不能验原签名，因此不能拿展示记录冒充原始重放数据。归档失败返回503，已提交的资金事务仍由幂等保护，重试不会重复入账。

```bash
uv run python scripts/replay-payment-notice.py --env-file .local/payment-application.env --notice .local/payment-notices/notice-HASH.json --output .local/payment-notices/replay-result.json --count 5
```

工具只向本机独立入口发送原通知至少5次，再改金额、保留原签名重放，要求400及PAYMENT_SIGNATURE_INVALID审计增加，入账审计仍1条。当前只通过临时密钥/实际PG自动测试，尚无本轮真实平台通知可重放。新单须在发送付款链接前延后该单的自动查单补偿，以证明由通知入账；这是验收用调度设置，不改变产品默认策略。

### 退款与账单实测

对上一笔已退款交易，确实再次向真实网关发送同一out_request_no的5次退款请求（没有走本地已退款短路），5次响应验签通过且fund_change=N；金额/订单/交易均匹配。随后独立退款查询REFUND_SUCCESS、金额和请求号匹配，库内仍1笔退款、1条成功审计。新可选amount_cents仅用于明确校验退款申请金额，默认仍全额；超过已付金额的请求本地返回REFUND_AMOUNT_EXCEEDS_PAID，未生成新退款记录，部分退款仍不支持。

已证实当前沙箱具备外部账单下载能力：前一日请求返回验签通过的地址，当日请求仍404。提供方给HTTP地址时，下载器只尝试同一受信主机/路径的HTTPS形式，证书校验保持开启，不作HTTP回退或跟随重定向；外部域名、身份信息、片段和HTTP非标准端口拒绝。已实际通过TLS下载前一日ZIP，1,017字节，原件只在 `.local/payment-bills/2026-10-28.bin`。

实测发现旧中文ZIP文件名编码与汇总CSV混在归档中，解析器已支持CP437标记的GB18030/UTF8名称，并排除汇总；退款请求号支持实际列名“退款批次号/请求号”。前一日明细0行、对应本地支付/退款0行，差异数0，**仅为空账单烟雾，未覆盖今天的交易**。不能写成“沙箱不支持外部账单”，也不能据这个0声称本次交易对账通过。当日交易仍需逐单验签查询，完整当日外账对账待账单可取后验收。


## 2026-10-29 P6 本轮开放已确认，入口启动并生成新单（Codex）

用户明确确认本轮TCP 18080已开放。同步仓库后重启回环API、启用并启动独立通知入口；就绪后/orders匿名401、通知GET405。未修改云端规则、既有系统Nginx或透明代理。通过私有演示顾客HTTP登录→新下单→生成支付链接，金额1分，发送当前会话成功。实际地址、签名链接、账号和令牌未输出或入仓库。

发送付款链接前，已将本轮订单的next_query_at延后一天，避免查单抢先入账掩盖真实通知验收；不是产品默认策略变更。首次只读观测payment pending、order pending_payment、通知0、仅PAYMENT_CREATED一条、库存9/预留1，尚无真实通知归档。此刻仍等待用户付款，不能写成通知验签或新单入账成功。私有状态/基线在.local/p6-notify-acceptance-20261029，公开脱敏记录reports/p6-2026-10-29/notify-acceptance-activation.json。

本轮无代码修改，沿用已通过214项测试/ruff/strict mypy的实现，不重复全量测试。下一步用户付款后核对通知与审计来源、原件归档，再至少5次重放和篡改金额验签失败；完成后告知删除本轮入站TCP 18080允许规则。容量和限频继续未验证，不进入P7。
