# S1 设计与预期（待负责人确认后执行）

2026-11-02，Codex。本文只写假设与实验设计，不写具体提升数字；测完再填 `S1.md`。

## 问题与现状

人工工作台要在百万工单里翻出“待人工”的工单，50 个座席同时领取不能重复。

P4 产品默认的领取已经是单条 CAS `UPDATE ... WHERE revision=:rev`（`workbench.py:325` 起），不会重复领取；列表是 `ORDER BY priority DESC, created_at, ticket_id LIMIT/OFFSET`（`workbench.py:428`），工单表只有主键与状态检查约束。方案要求把“先查后改”的两步领取作为**实验基线开关**，不修改产品默认行为。

## 假设（方向，不写数字）

1. **部分索引**（只含 `state='waiting_human'`，排序键 `(priority DESC, created_at, ticket_id)`）能让待人工列表的扫描量与索引体积随“待人工比例 5%”而不是全表增长。
2. **游标分页**（keyset：`(priority, created_at, ticket_id)` 严格递增元组）消除 OFFSET 的线性跳读，翻到第 1 万页的时间不随页号线性增长。
3. **SKIP LOCKED + 版本号**在 50 座席并发下把领取冲突从“重试/重复”变成“跳过已被锁定的行”，领取吞吐上升、重复领取为 0；代价是不保证严格优先级与公平性。

失效条件：查询谓词与部分索引不一致时优化失效；SKIP LOCKED 可能让低优先级工单长期饥饿；keyset 需要稳定的排序键，`priority` 变更会导致翻页跳项。任何一条出现，都在报告里写明，不当作通过。

## 基线定义

- 数据：100 万工单，5% `waiting_human`（约 5 万），其余 resolved/claimed 等；`priority`、`created_at` 按 ECD 意图分布合成，固定种子。
- 列表：OFFSET 分页 + 仅主键。
- 领取：两步 `SELECT ... LIMIT 1` 然后 `UPDATE ... WHERE ticket_id=:id`（实验开关 `bench_claim=two_step`），此路径**只用于对照**，产品默认仍 CAS。
- 数据库：独立 `agent_ops_p10_s1`（从 `agent_ops` 迁移 + 只复制 support/commerce 结构，不写生产库），PostgreSQL 16，记录 `shared_buffers`、连接数、CPU/内存配额。

## 优化版本

- `CREATE INDEX CONCURRENTLY ... ON support.tickets (priority DESC, created_at, ticket_id) WHERE state='waiting_human'`。
- 列表改为 keyset：`WHERE state='waiting_human' AND (priority, created_at, ticket_id) < (:p, :c, :t) ORDER BY ... LIMIT :n`。
- 领取改为 `UPDATE ... WHERE ticket_id = (SELECT ticket_id FROM support.tickets WHERE state='waiting_human' ORDER BY ... FOR UPDATE SKIP LOCKED LIMIT 1) AND revision=:rev RETURNING ...`，记录跳过的行数。

三项分别独立开关，便于反向验证。

## 实验矩阵

| 批次 | 内容 | 次数 |
| --- | --- | --- |
| A/A | 基线 vs 基线（同配置） | 5 |
| 基线 | OFFSET + 两步领取 | 5 |
| 优化 | 部分索引 + keyset + SKIP LOCKED | 5 |
| 消融 | 只开部分索引 / 只开 keyset / 只开 SKIP LOCKED | 各 3 |

- 同一轮内 A/B 交替执行，成对比较；每轮前清空租约并重建 5 万待人工行，保证起点一致。
- 并发：50 个座席持续领取 60 秒；列表查询由另一组 20 个客户端并发打 4 个页深（1 / 100 / 5000 / 10000 页，每页 50 条）。
- 指标：列表 p50/p95/p99、第 1 万页耗时、领取吞吐（成功领取/秒）、重复领取次数、跳锁次数、索引体积（`pg_relation_size` + `pg_indexes_size`）、CPU 与 DB 连接等待。
- 噪声：A/A 的 p95 与吞吐离散度作为下界；提升必须同时超过 10% 与该噪声的较大者，否则写“无显著收益”。

## 产出

- `docs/optimizations/S1.md`：现象、假设、改动、前后对比、代价、失效条件、复现命令。
- `docs/measurements/2026-11-xx-s1/`：每轮原始 JSON（延迟分位、吞吐、重复数、执行计划摘要、索引体积）。
- 反向验证：逐项关闭索引/keyset/SKIP LOCKED，确认收益来源，不只报总和。

## 风险与需要确认

- 100 万行 + 50 并发在 7.5 GB / 4 vCPU 上是否够用；若 DB 先饱和，结论只能写“受资源限制”，不外推。
- 两步领取基线会产生重复领取，报告要如实写出次数；它不代表产品行为。
- 部分索引的创建时间与 WAL 体积要单独记录。
- 需要确认：是否同意新建 `agent_ops_p10_s1` 独立库、50 座席 × 60 秒、每档 5 次的时长安排。
