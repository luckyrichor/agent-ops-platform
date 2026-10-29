# P7 整租户删除适配器权限核查与收窄

2026-10-29，P7已验收后的跟进；对应固定memory版本1e2730c，不改其接口。

核查结论：原先删除适配器与memory API共用p7_memory_runtime角色，继承agent_memory_app九表SELECT/INSERT/UPDATE/DELETE；不是超级用户或BYPASSRLS，但权限超过删除需求，且memory侧审计随租户删除后没有独立删除审计。已收窄并在运行实例验证：

| 项目 | 当前权限/行为 |
|---|---|
| memory API运行角色 | p7_memory_runtime；撤销agent_memory_app成员资格，九表SELECT/INSERT/UPDATE；只有idempotency_records允许DELETE（固定实现清理过期幂等记录所需） |
| 删除管理角色 | p7_memory_purge；独立登录/独立随机凭据，NOINHERIT、非owner、非super、NOBYPASSRLS、NOCREATEDB/NOCREATEROLE；不属于agent_memory_app |
| 删除角色对象权限 | 固定九表SELECT/DELETE；只有memories.current_version_id、successor_id两列UPDATE；public schema USAGE、本专属库CONNECT；没有INSERT、TRUNCATE、TRIGGER、REFERENCES、DDL或迁移表写权限 |
| 九表 | evidence、memory_embeddings、memory_versions、memories、events、outbox_messages、jobs、idempotency_records、audit_logs |
| RLS | 固定memory schema的九表ENABLE/FORCE RLS保留；事务局部app.current_tenant_id由平台认证顾客映射得出，再带tenant_id条件删除；无租户上下文看不到行；其他顾客的单条/版本/检索/上下文不可见测试继续通过 |
| 调用入口 | 产品代码只有DELETE /v1/commerce/memory调用delete_all，再调用内部_delete_all；导购读写走SDK和短期JWT，不使用memory_engine；测试直接调用是隔离验证，不是另一产品入口 |
| 审计 | 新迁移0009_memory_delete_audit，在平台库customer_memory.deletion_audit只追加started/completed/failed，记录请求UUID、平台顾客ID、时间、结果；不记正文/租户UUID/令牌。应用角色仅INSERT及identity sequence USAGE，没有SELECT/UPDATE/DELETE；审计不会随memory租户删除而消失 |
| 凭据 | memory API读取.local/p7/application.env；平台API读取.local/p7/application-with-purge.env，专用删除URL源文件.local/p7/purge.env。均600，父目录700；memory API配置不含删除凭据。签发私钥仍.local/p7/memory-signing.pem |

专属库public schema撤销PUBLIC CREATE，没有更改其他memory评测库/角色或相邻仓库未提交内容。原开发维护owner连接仍仅在私有维护文件中，产品不使用；没有输出或提交凭据。平台库只升级已授权P2实例到0009，未触碰P0和P6财务库。

安全边界必须如实理解：删除角色是受限**跨租户管理能力**，本身可以设置任意tenant GUC；RLS不等于把该角色永久绑定一个顾客。可信边界是认证后的平台user_id映射、私有后端和唯一删除入口；前端不能提交tenant或数据库URL。两个服务当前仍同OS账户，独立数据库凭据不是OS级密钥隔离，平台被攻陷仍有管理风险。升级schema/引入worker必须重新验证九表范围，不能照搬此适配器。

审计先写started才操作，写失败不执行删除；跨库删除失败留deleting门控并写failed，可重试。平台数据库在操作中完全不可用时，结果审计可能无法落盘，started可留作未完成尝试；不能声称具备跨库原子审计或独立防篡改日志。在线删除不擦除旧WAL/离线备份，审计保留平台ID用于责任追溯，不宣称删除所有关联元数据。

重建服务单位（使用已准备好的私有文件）：

```bash
uv run python scripts/p7-services.py --env-file .local/p7/application-with-purge.env --memory-env-file .local/p7/application.env
systemctl --user restart agent-ops-customer-memory agent-ops-p7-api
systemctl --user is-active agent-ops-customer-memory agent-ops-p7-api
```

不要重新跑首次建库脚本；独立角色授权规则见上表，由已有维护身份在专属库执行，凭据不写命令行/仓库。迁移仅用维护身份，产品角色不提升。监听保持回环，P6入口停用。权限实测在docs/reports/p7-followup-2026-10-29/role-verification.json；测试数字与失败返工见本轮progress。
