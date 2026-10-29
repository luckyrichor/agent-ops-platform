# P7 导购、顾客记忆和运行手册

2026-10-29，Codex。范围仅 §6 P7 与已批准身份/删除前置条件；S3 优化、后续阶段不在本次范围。

## 身份与数据流

顾客平台登录 → 从已验签 principal 取 customer_user_id → 懒创建持久映射 → 独立 memory 租户/用户 UUID → 后端专用 RS256 委托令牌 → 固定版 memory HTTP API。不会为 P2 百万用户预建租户。每个租户是顾客隔离单元，不能用来共享顾客记忆。身份来自服务端数据库，不接受客户端 tenant/sub，单条记忆编号也不能改变当前租户。

固定 SDK/独立 API 均为 agent-memory@1e2730c。`customer_memory.identities` 只在顾客实际发起导购时插入；(user_id) 唯一，UUID 唯一，分配先提交，再用行锁串行化同顾客调用和删除，避免共享可变 HTTP 身份。独立服务只接受自己的 issuer/audience；平台登录 JWT 不发送给其他服务。每次调用新建带当前顾客身份的 SDK，不共用可变 Authorization。

memory 记忆使用 user_global scope，subject_user_id 等于映射用户；JWT 无 admin、无共享 workspace 权限。固定版单条读取只按租户定位，因此独立租户是必要的隔离边界；搜索、context、单条、版本和生命周期均由真实 API/RLS 隔离测试覆盖。

### 令牌、密钥与轮换

默认有效期300秒，可配置60至600秒。令牌有 iss/aud/tenant_id/sub/iat/nbf/exp/jti，purpose=commerce-memory；用途通过专用 audience 和专用服务公钥校验绑定。只授 memory:read、memory:write。令牌仅在后端内存，不出浏览器、仓库、报告或日志。

实际签发私钥 `.local/p7/memory-signing.pem`，公钥 `.local/p7/memory-public.pem`，文件600、目录700；独立密钥不复用平台 HS256 或现有 memory 评测密钥。运行配置拆分为 memory API 的 `.local/p7/application.env` 与平台 API 的 `.local/p7/application-with-purge.env`（均600）。删除角色与审计收窄详情见 [删除权限核查](p7-memory-purge-review.md)。

轮换采用维护窗口（固定版单公钥，无假装多key热切换）：停止 P7 业务 API 和专用 memory API，等待在途请求结束；生成新的 RSA 至少2048位密钥到临时600文件，原子替换私钥/公钥；先启动 memory API，再启动业务 API。旧令牌不再通过新公钥验证；映射、现有记忆和幂等操作保持不变。验证新令牌成功、旧签名失败。不要改正在运行的其他 memory 服务公钥。异常时只恢复本次备份密钥对并重启这两个服务。

### 幂等与失败

平台持久化 (user_id,run_id) 操作、规范化请求摘要、首次结构化 memory 写入正文及结果。memory 使用稳定 commerce:v1:<memory-user-uuid>:<run-uuid>:remember，复用其 (tenant,key) 事务锁/内容摘要/409。相同操作改变输入返回409，不同顾客相同run_id互不影响。首次写入内容在网络调用前独立提交，响应丢失仍使用相同正文/key重试；不因库存变化改变同键正文。已成功操作返回保存的结果快照，下单仍须重新校验实时价格和库存。

固定版幂等记录七天过期；平台不对超过七天仍未知的操作盲目重发，返回 MEMORY_OPERATION_EXPIRED；已有成功结果不会跨期重复写。无法确认的过期写入需要另行核查，不能改run_id冒充重试。身份锁与操作表外键解耦（外键指向稳定 commerce.users），避免跨连接提交幂等记录自阻塞。

memory 不可用时读取/写入分别降级，商品流程仍读数据库，不切共享身份。配置全部缺失时不启用记忆，状态 not_configured；部分配置缺失或不安全配置启动拒绝，不能伪装启用。未登录导购/删除均401；客服和管理员不作为顾客访问这些接口。

## 删除自己的全部记忆

DELETE /v1/commerce/memory，只允许登录顾客，顾客不能指定目标租户。网页提供明确确认按钮；未登录401，配置缺失200/status=not_configured，不创建身份；从未使用过的顾客返回 deleted/deleted_tenants=0。

固定 API 没有整租户删除端点。本阶段没有修改 agent-memory 业务 API；使用平台侧固定版本管理适配器连接**专用** memory 数据库，以 NOSUPERUSER/NOBYPASSRLS 非owner角色并设置当前租户，在事务中清理 memories、versions、embeddings、evidence、events、outbox、jobs、idempotency、audit 共九表。仅本平台服务使用该库，不运行外部抽取/embedding workers。此适配器与固定schema耦合，升级前必须重新验收，不能复用到其他 memory 数据库。

流程先提交 deleting 门控，等已持锁的该顾客调用结束；新调用返回503/MEMORY_DELETION_IN_PROGRESS。清理成功后擦除平台操作正文/结果/摘要，保留无正文的旧run_id拒绝标记，并清空映射UUID、增加generation；下次实际新操作才分配新租户。旧操作重试409/MEMORY_OPERATION_PURGED，防止删除后重建原记忆。删除失败保留门控，返回503，可用同一删除接口重试；跨两个数据库不伪称分布式原子事务。其他顾客不受影响。

这是在线数据删除，不承诺擦除数据库历史WAL或已存在的离线备份；备份保留与安全销毁属于后续部署治理。既有订单、客服记录不因记忆删除而清除。

## 真实商品与缓存开关

`0008_catalog_search` 给 commerce.products 增加英文 PostgreSQL tsvector 生成列及 GIN 索引。plainto_tsquery 参数化，结合类目、价格、JSON属性、有货条件；价格/运费读商品表，可用库存为stock-reserved。无任意SQL或语义检索，空结果就是空结果，不补假商品。英文词干和停用词由 english 配置处理；中文只支持现有分词得到的词元，不声称实现中文语义理解。

固定编排四角色：catalog → pricing/shipping 并行 → recommendation。从检索前100候选中选含运费在预算内且总价最低的一项；不声称在所有商品上做全局最优推荐。region 保留为上下文，运费直接读真实 freight_cents，不再用演示地区定价。SSE 发连接/心跳/最终结果，不冒称模型逐token生成。

`AGENT_OPS_CATALOG_CACHE=off|on` 是缓存接口开关。off 直读，on 经透传适配器读库；本阶段两态均实时读取，无实际缓存、命中率或性能收益声明。多级缓存/TTL/singleflight/一致性实验留 S3。

数据使用已验收 P2 的200000商品，未使用 P5 六商品作为 P7 验收数据。P2 原有合成/来源标记保持，不能把合成标题或固定库存说成真实商家库存分布。迁移前备份 `.local/p7/p2-before-p7.dump`，301445708字节、600；只新增schema/全文索引，不重导P2数据；P0及P6沙箱财务库未升级。备份恢复演练未验证。

## 接口与配置

- GET /products/search：query、category_id、min_price_cent、max_price_cent、available_only、limit、offset；普通偏移分页，返回 items/next_cursor（搜索场景cursor值为下一offset）。
- POST /v1/commerce/runs、/stream：现有 request/category/budget_cent/region/run_id/memory_id，增加可选 category_id/min_price_cent/attributes。身份始终来自登录principal。
- GET /v1/commerce/availability：登录顾客可见 catalog_enabled、memory_enabled。
- DELETE /v1/commerce/memory：上述整租户删除。

配置名：AGENT_OPS_CUSTOMER_MEMORY_URL（回环HTTP）、AGENT_OPS_CUSTOMER_MEMORY_DATABASE_URL（memory API运行连接）、AGENT_OPS_CUSTOMER_MEMORY_PURGE_DATABASE_URL（仅平台删除适配器使用的独立受限角色）、AGENT_OPS_CUSTOMER_MEMORY_KEY/PUBLIC_KEY（私有文件路径）、ISSUER、AUDIENCE、TTL；这些名称均带 AGENT_OPS_CUSTOMER_MEMORY_ 前缀。平台 PG、认证配置沿用P2私有环境；AGENT_OPS_CATALOG_CACHE 控制接口路径。

## 运行、访问与验证

持久专用库容器 agent-ops-p7-memory，卷同名，数据库仅回环55435；不得删除卷。专用memory API18886、P7 API18885、客户网页/工作台5175，均仅127.0.0.1；未开放公网、防火墙或系统代理。

```bash
# 已准备好私有配置及对应迁移后安装/激活本轮独立服务
uv run python scripts/p7-services.py --env-file .local/p7/application-with-purge.env --memory-env-file .local/p7/application.env --activate
systemctl --user status agent-ops-customer-memory agent-ops-p7-api agent-ops-p7-web
systemctl --user stop agent-ops-p7-web agent-ops-p7-api agent-ops-customer-memory
# 停止后重启：先memory，再API，最后网页
systemctl --user start agent-ops-customer-memory agent-ops-p7-api agent-ops-p7-web
# 用户电脑执行（tx使用本人的既有SSH别名）
ssh -N -L 5175:127.0.0.1:5175 tx
# 然后浏览器访问 http://127.0.0.1:5175/customer 或 /workbench
```

支付公网入口保持 inactive/disabled，不能因本轮服务启动就推断任何云规则开放。P7 不启用 P6 付款或重新发起支付。

验证命令：`AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q`，`uv run ruff check src tests`，`uv run mypy --strict src`，`npm --prefix frontend run build`。P7 Playwright 需显式设置P5_BASE_URL/P5_ARTIFACTS及P7_ROSTER_PATH/P7_KEYWORD_PATH，仅私有路径传入账号/验收关键词，禁止写入测试源码或日志。截图只存.local，遮罩账号/凭据。

## 必须保留的限制

租户数量增长时的开销：**未验证**。当前只验证少量真实身份、并发与删除，不从P2百万用户规模推算租户容量。缓存容量、限频与负载能力未压测；语义检索/模型导购、S3优化、P8及以后未实施。P6账单与查单限制继续沿用 docs/p6-payments.md，不得笼统写“对账零差异”。
