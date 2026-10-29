# P7 前置：登录顾客 memory 身份方案（待确认，未实现）

2026-10-29，Codex。依据方案 §6 P7 与用户本轮要求，先设计、待确认后实现；当前 HTTP memory 状态仍为 not_configured。

## 已核实的契约

平台锁定 SDK 为 agent-memory@1e2730c；相邻源码为 2fae08b。两版 api/auth.py 均只接受 RS256 JWT：issuer、audience、tenant_id(UUID)、sub(UUID)、permissions。没有“共享服务令牌 + 任意平台用户 ID 请求头”的授权协议。不得转发平台 HS256 登录令牌，也不能由浏览器选择 memory 身份。

固定版 application/explicit_memory.py 的 _required_record 依赖 tenant_id 查单条记忆，并不在该路径再次验证 subject_user_id。因此不能只在共享租户里用 user scope 就宣称已完成隔离。已核实的隔离边界应落到租户 JWT + RLS。记忆创建幂等键按 (tenant_id,key) 存储，带事务锁、内容摘要；同键不同内容返回409，记录有效期7天。事件摄取另有 (tenant_id,idempotency_key) 及会话序号唯一约束。

## 推荐：服务端委托身份，每个顾客独立 memory 租户

1. 平台先验证顾客登录。从已验证 principal 的稳定平台 user_id 查服务端映射表：唯一 (deployment_namespace,platform_user_id) → 独立 memory tenant UUID 和 user UUID。UUID 首次生成并持久保存，唯一约束防并发重复；不使用邮箱、标签页、run_id 或客户端提交的 user_id 生成身份。环境命名空间固定，备份恢复不重建映射。注销/换号立即清除该请求与会话引用。
2. 后端为此次调用取得短期、仅供 memory 的 RS256 委托 JWT（建议5分钟），tenant_id/sub 来自映射，权限仅 memory:read、memory:write，无 admin、无共享 workspace。业务记忆只写 user_global，subject_user_id 固定为映射用户。请求 DTO 不接受身份覆盖；单条 memory_id 也只能在当前顾客租户下查，别人的 ID 返回不可见。
3. 委托令牌不是平台访问令牌，不返浏览器、不写日志、不进入商品缓存。memory HTTP 客户端按请求绑定该身份，不共享可变 Authorization。任何 memory/session/run 缓存都必须含租户和用户，绝不复用全局 MemoryClient 或原 commerce workspace。
4. 签发是明确的信任授权：持有签名私钥的后端能够签发所信任租户身份。推荐为平台使用独立的 memory 服务配置/隔离实例与专用 RS256 密钥、issuer、audience，不复用现有评测服务或平台 HS256 密钥，不改动正在运行的 agent-memory 工作。专用私钥仅 .local/600；memory 持公钥。当前没有现成的凭据交换端点，不能假装可以从共享服务令牌自动换用户身份。若以后必须共用单一现有实例且要多 issuer/受限委托，需要另报 agent-memory 接口或鉴权变更，获得批准后再改。
5. 独立实例使用既有 JWT/API/RLS 契约，不要求修改 agent-memory 业务接口。具体启动/资源与配置变更须在确认本方案后准备，不能把计划写成已部署；双方契约文档届时同步。

## 复用幂等机制

后端先持久化导购操作，验证其顾客/会话归属，并保存规范化请求摘要和结构化写入内容。顾客重复请求使用该稳定操作 ID；同键改变输入在平台拒绝409。memory key 采用版本化命名空间，例如 commerce:v1:<mapped-user-uuid>:<operation-uuid>:remember；重试/重连保持相同 key、相同内容，不能每次生成 run_id 当重试键。不同顾客同一客户端 key 因服务端操作归属与租户隔离而互不影响。摘要包含 schema 与实际写入内容，不能因实时库存改变而悄悄换内容重试。

复用 SDK remember 的 Idempotency-Key 和服务端现有摘要/409/事务锁，不新造另一套 memory 去重接口。鉴于现有7天有效期，平台需保存写入结果与操作状态，已成功操作不跨期重新写；超过期限仍未知的写入不盲重放，明确标待核查。P7 不额外引入会话事件抽取链；若以后摄取事件，复用稳定 event key 和持久化 session/sequence，禁止由浏览器指定任意会话。

## 未登录和失败

未登录的现有 /v1/commerce 保持401，不创建匿名共享 memory 租户、不读写记忆；公开商品浏览可以无登录但不触达 memory。若以后另开匿名导购，需要另定生命周期与授权，本次不做。配置缺失继续 not_configured；服务不可用只降级记忆读写，不切到共享身份、不回显其他顾客 memory_id，商品和库存仍读业务库。

## 批准后的验收与 P7 边界

先验证两个顾客相同输入/幂等键并发、互猜 memory_id、标签页切换、流式重连、401/失败降级、令牌泄露检查。再按 P7 接 PostgreSQL 全文+结构化过滤及真实库存，使用已验收 P2 的20万商品数据；不使用 P5 六商品。缓存只留可开关接口，验证开关两态结果一致，S3 多级缓存/容量实验仍留后续。

本文件仅方案。身份映射、签发、独立实例、memory 接入和 P7 商品检索均未实现，等待用户确认。
