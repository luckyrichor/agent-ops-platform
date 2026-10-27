# AGENTS.md

最后更新：2026-10-27（北京时间）

本文件为在本仓库工作的编码 agent 提供指引（Claude Code 读 `CLAUDE.md`、Codex 读 `AGENTS.md`，两者都指向这里）。

## 这是什么

Agent 应用与可观测评测平台。电商场景的多 Agent 任务拆解、工具调用、高并发流式处理，配套追踪与评测体系。

三个月求职计划六项目之一（原编号 ③），对应岗位 **05、07、10、11、14**，另含 **08** 作为子方向。总计划见 [workplan-docs](https://github.com/luckyrichor/workplan-docs)，岗位原文以那里的 `岗位要求原文.md` 为唯一依据。

**当前状态：P2 已由负责人验收；P3 已验收并于 8900974 提交推送；P4 后端及两库 0005 迁移已完成，最终 163 项测试通过，等待 P4 验收，禁止进入 P5。两个持久库均已到 0004；P2 建有 6 个演示账号、种子工单按确定会话关联补归属；P0 历史测试工单保持无归属。导购两个 HTTP 入口必须 customer 登录，平台 JWT 不转发到 agent-memory；现有 memory 无服务凭据加平台用户 ID 协议，HTTP 记忆状态为 not_configured（P7 前缺口）。完整证据见 [P3 收尾交付](docs/reports/p3-followup-2026-10-26/delivery.md)，135 项测试通过、0 跳过。业务 API 未常驻；Jaeger 六组件栈已运行。**

## 分工模式：偏产出

Claude 主写实现，用户做设计决策（在给出的 2–3 个方案里选并说明理由）。用户不需要写代码。

## 重点放在哪

不做 Agent 入门演示——用户这块已经熟悉。直接压 JD 真正区分人的地方：

- **可观测**：多 Agent 任务拆解后一条请求散成调用树，trace 如何串联、出错时如何定位到具体某一跳的工具调用
- **高并发流式**：SSE/WebSocket 的背压、慢消费者隔离、取消传播
- **评测**：可复现的评测集与判定口径，而不是看几条输出下结论
- **集成 `agent-memory`**：作为记忆服务的第一个真实调用方，打通接口时序、失败降级与幂等

## 岗位 08 子方向（2026-09-19 决定）

岗位 08（字节 AI 全栈-客服平台）此前未分配到任何项目，现决定**挂在本仓库下作为子方向**，不新增独立项目。

客服平台与本项目的电商多 Agent 场景有天然重合（工单/会话处理、工具调用、人工接管），可复用同一套 trace 与评测基建。**但 08 的 JD 含全栈/前端要求，本项目不做前端**，这部分不声称覆盖——具体差距在技术方案里如实列出。

## 依赖关系

通过 HTTP API 调用 [`agent-memory`](https://github.com/luckyrichor/agent-memory)。**两者保持独立边界**：`agent-memory` 提供记忆能力，本项目负责业务任务与平台能力。改动涉及接口时，两边的文档都要同步。

`agent-memory` 可常驻在 tx 服务器上供本项目连接（那台机器 `Linger=yes`，用户级 systemd 服务能常驻）。

## 覆盖边界

对应岗位是**目标关联**，不代表单个电商演示自动覆盖全部要求。要对应岗位 10 的数据 Agent 能力仍需数据资产或语义层实践；要对应 07 的研发工具要求仍需开发者产品方向的证据。**不要在文档里笼统声称已覆盖。**

## 技术栈

Python 3.12 + FastAPI + uv 锁文件；`bash scripts/bootstrap.sh` 重建，pytest/ruff/mypy strict 验证。确定性 planner 和本地角色工具先验收编排，M2 通过 HTTP SDK 集成 memory。

## 所有项目共同的约束

这些是用户明确要求的，优先级高于一般工程习惯：

- **区分事实与推断**：JD 原文明确写的、与「行业常见要求／补充推断」必须显式区分。允许联网补充，但要标注来源性质。
- **不夸大**：不把规划写成已完成成果，不把本地测试数据包装成生产规模，不把加分项改写成硬性门槛。
- **不预设降级**：即使用户当下没时间反馈，也按既定方向持续推进产出，不因「时间可能不够」提前砍掉或缩水——取舍由用户自己做。
- **不安排模型算法、训练、微调、推理引擎优化方向的学习。**
- 不需要重新确认用户背景：Python 最熟练，C++/C#/Go 有基础，Agent 开发已较熟悉。直接进入框架层和有深度的工程问题，不安排语言零基础或入门教程。
- **AI 产出的代码不等于可以写进简历。** 用户要能在面试里讲清每个关键设计取舍，偏产出的项目要同步维护 `docs/design-decisions.md`。

## 多机协作

三台机器：Mac、`tx`（Ubuntu 服务器，常开）、`luowindows`（Windows，引擎唯一机器）。GitHub 是唯一事实源。

- **开工 `git pull`，收工 `git push`**，不留未推送的提交过夜
- **同一个项目同一时间只在一台机器上改**
- 密钥永不进仓库，放仓库内已 gitignore 的 `.local/`

**动环境之前先读 [`workplan-docs/环境与踩坑记录.md`](https://github.com/luckyrichor/workplan-docs/blob/main/环境与踩坑记录.md)** —— 三台的规格、三种不同的代理机制、以及十条已经踩过的坑都在那里。其中两条最容易再犯：三台文件系统大小写敏感性不一致（tx 敏感，另外两台不敏感）；PowerShell 5.1 的 BOM 规则（读 `.ps1` 必须带 BOM，写文件绝不能带）。

## 进度记录

进展写在 `docs/progress.md`：**只写已发生的事，不写计划**；失败和返工也要记，那是面试时最有料的部分。汇总到 `workplan-docs/进度总览.md`。

## Docker 运行位置（用户明确要求）

**需要 Docker 时默认跑在 `tx` 服务器上**，不要在 Mac 或 Windows 上起容器。

确有必要在本地跑时，**必须先征得用户同意**，不要自行决定。

tx 上 `ubuntu` **已加入 `docker` 组**（2026-09-19），docker 命令不需要 `sudo`，Testcontainers 也可直接用。该机内存只有 7.5G，多个服务同时跑之前先看 `free -h`。

### 在 tx 上跑 Docker／测试前，必须先同步代码

本地改完直接去 tx 跑，而 tx 上还是旧代码 —— 结果是「测试通过」测的其实是旧版本，**这种假通过比失败更危险**。

用 `workplan-docs/scripts/run-on-tx.sh` 代替手工 ssh，它强制执行正确顺序：

```bash
bash workplan-docs/scripts/run-on-tx.sh agent-memory 'uv run pytest -q'
bash workplan-docs/scripts/run-on-tx.sh agent-memory 'docker compose up -d postgres'
```

它会：本地有未提交改动就拒绝 → push → tx pull → **校验两边哈希一致** → 才执行。哈希比对是最终判据，不是「命令有没有报错」。脚本还顺带剥掉了腾讯云登录横幅。

## 记录规范：新内容写到哪儿

**本仓库的正文只维护 `AGENTS.md` 一份**，`CLAUDE.md` 永远只是几行指路。两份都写正文必然漂移，**不要往 `CLAUDE.md` 里加任何内容**。

| 内容类型 | 去处 |
|---|---|
| 本仓库的指引、约定、边界、分工方式 | 本仓库 `AGENTS.md` |
| 环境、机器、工具链、跨平台的坑 | `workplan-docs/环境与踩坑记录.md` |
| 已经发生的进展、验证结果、失败与返工 | 本仓库 `docs/progress.md` |
| 关键设计取舍及其理由（面试要讲的） | 本仓库 `docs/design-decisions.md` |
| 排期、工时预算、里程碑 | `workplan-docs/总节奏表.md` |
| 岗位要求本身 | `workplan-docs/岗位要求原文.md`（唯一事实来源，不要在别处改写） |

写进「环境与踩坑记录」时，用 **［实测］**／**［预警］** 标注区分「已验证的事实」和「未触发的已知风险」，不要把推断写成结论。


## 当前模型接入与运行约定

可选 llm planner 已实现，四角色与依赖集合固定；本地工具使用显式类目/预算/地区，模型不提取自然语言需求、不自由选择工具。它验证真实模型延迟、限流、失败及取消传播，不能称为自主规划能力。pricing/shipping 只能依赖 catalog，不允许模型添加依赖使其串行。

mini（doubao-seed-2-0-mini-260428）与 lite（doubao-seed-2-1-lite-260915）均已通过3/3真实应用初验；默认模型及服务器私密环境使用lite。旧 ModelNotOpen 阻塞已解决，原失败与成功证据只在 docs/progress.md 和 measurements 中作为历史保留，不作为当前指引。重复评测与边界见 docs/llm-planner.md。

未配置模式默认 deterministic；固定业务图日常执行优先使用该模式，无模型延迟/费用。llm 是显式在线验证模式，每次请求都会调用、不缓存、不静默回退。`.local/ark.env` 保存私密凭据和llm配置，需要用户显式source，不入GitHub/ZIP。离线测试强制 deterministic、移除 ARK_API_KEY，模型测试显式mock；真实评测脚本才可调用已授权模型。Jaeger 已常驻，业务API尚未常驻。

## 2026-10-08 第二轮评审补充

构建不再强制 sibling agent-memory：使用 HTTPS Git 固定 1e2730c（M6 Context Builder），pyproject/uv.lock 与实际依赖一致；该版本含数字 UUID 内容误判修复。首次安装需要 Git 与仓库读取权限，仍安装完整 SDK 所属包。实际数据库联调需源码迁移，可指定 AGENT_MEMORY_SOURCE；必须与安装 SDK commit 一致且干净，无源码时独立环境只跳过该用例。当前工作区完整58测试通过。HTTP SERVER 4xx 不标 ERROR；422/框架错误 JSON 返回 trace_id；读写降级独立保留，组合状态见 README。


## 当前追踪/记忆契约补充

trace_id是关联号；trace_sampled=false不能期待后端查询，true也不是导出投递确认。header/JSON/SSE均提供采样标记。最新源码后端复验见tracing-review.json，历史报告须对照生成源码摘要。记忆内容不存原话/run_id/memory_id；run_id仍作为幂等key持久元数据，措辞不同而结构化内容相同应重放。业务API未常驻、入口traceparent与跨服务传播未实现，M5客服规则路由/工单路径与47条标注评测已实现，见docs/customer-support.md；固定图provider评测仍不称规划可靠性。


## 2026-10-18 首次 B0 交付范围（Codex）

用户附件原件在docs/agent-ops-platform面试级完善方案.md，保持署名/原文，不改写计划。用户本次明确限定“修已知错误、建立B0，首次交付后再决定后续”。附件P0/P1却是环境栈/PG数据层，与消息中的阶段解释不同，已请求澄清；本次以明确描述的修错+B0为范围。不要将本次报告写成环境栈/PG已完成，后续数据库、前端、数据集和支付等待用户审阅后的指示。原baseline-v0与修复后baseline-b0标签、历史数据保留，不重写/覆盖；基线不得故意劣化。

B0入口docs/baselines/b0/README.md；HTTP脚本scripts/benchmark-b0.py必须离线确定性、localhost、与业务核分离、至少五轮并记录原始数据/源码hash。LLM原本已默认8槽，并非无并发上限；暂无入口排队界限/熔断，不能拿不实方案文字当实际基线。47条标签仅回归，不当真实准确率。本轮没有默认授权大批真实模型调用、外网暴露或改其他项目范围。

## 2026-10-21 P0/P1继续开发授权

负责人已明确“继续做P0/P1”，覆盖此前首次B0交付后的暂停范围。按附件第六节只做六组件环境栈与support PG数据层；P2起各阶段仍按后续指令。baseline-v0、baseline-b0标签及docs/baselines/b0原始结果不可覆盖；保留内存版开关。PG运行角色与迁移角色分离，事务幂等/事件追加/版本CAS不得退回进程内锁。P0新compose复用既有Jaeger项目和external数据卷，只绑定本地端口；不要down -v或删除历史追踪。应用不会自动常驻或暴露公网。

## 2026-10-21 最新用户方案与范围

最新原件 docs/agent-ops-platform面试级完善方案.md，旧版保留 docs/plans/archive/，SHA256 见 docs/plans/source-2026-10-21.json。先补 P1 的 baseline-b1（原 B0 脚本、参数/场景与五轮，工单切 PostgreSQL），单独报告后继续 P2；仅 P2，不提前做 P3及以后。UserBehavior 缺登录文件时不登录/注册、不偷换数据源，完成其他独立部分并报告缺口；P2 数据原文仅本地。P2 可建立不含密码/登录功能的业务用户ID供订单外键使用，认证和公网 API 权限仍属 P3；不得把 P2 本地后端接口称具备身份隔离。不可替负责人标注 ECD 意图，代理标签/未知分布须如实说明并等待决定。

## 2026-10-21 ECD 代理权重批准

负责人允许“规则代理，非人工真实意图分布，首片段说话人未确认”，仅作合成工单类别占比；所有使用处与数据清单、工单行、事件载荷、每文件 provenance 和文档必须带该完整说明。等待人工占比独立可配置，后续 S1 在多档比例检查稳定性，本轮不执行 S1。代理不能进入路由准确率/评测真值。P8 完成负责人审核的标注后替换权重并记 progress；说话人快速检查不足以确认时保持未确认，不代替人工标注。详见 docs/ecd-proxy-policy.json。

## 2026-10-23 P2 UserBehavior 后续执行口径

最新版方案继续位于 docs/agent-ops-platform面试级完善方案.md，原 2026-10-21 版本已归档，实际 SHA256 与两处 A/A/噪声判据变化见 docs/plans/source-2026-10-23.json。用户已提供完整 UserBehavior 附件，只补 P2，不进入 P3。不得用全量重导覆盖已验收商品、订单；仅增量更新独立 P2 库的用户活动权重并生成私有离线事件。既有订单用户关联仍是结构性轮转，不冒称拟合；源时间戳异常如实计数、不悄悄清洗。正式验收仍由负责人决定。


## 2026-10-23 新方案再次归档

当前原件路径不变，最新来源为 docs/plans/source-2026-10-23-v2.json，统一索引 docs/plans/README.md。旧原件与旧来源记录保留。本次只归档；新增S3前置分布/LRU命中率校验尚未执行，不代表已授权启动P3或S3。


## 2026-10-23 P3 认证与权限（Claude Code）

负责人确认 P2 已验收后，只授权了 P3。P3 已实现：邮箱密码登录，customer/agent/admin 三种角色，迁移 0004_auth，详见 docs/auth.md 与 design-decisions 的 P3 节。共享 SUPPORT_HUMAN_TOKEN 已取消；postgres 模式必须配置 AGENT_OPS_AUTH_SECRET。P2 的 100 万用户标识仍然没有凭据，演示账号只能通过 `python -m agent_ops.demo_accounts` 从 .local 名单创建。该阶段的 /v1/commerce 登录缺口已在 2026-10-26 收尾中补齐，见下节。P4 及以后（队列、抢单、推送、统计、前端、支付、登录限流）都没开始，不要写成已完成。


## 2026-10-26 P3 收尾交接（Codex）

### 本轮范围与版本

负责人在消息 om_x100b62c15245d4acb4f756e009702f7 明确授权两库迁移、P2 演示账号、运行 stack.py 生成签名密钥，以及 A–D 四项收尾，完成后必须停在 P4 前。新附件与现原件逐字节一致，来源见 docs/plans/source-2026-10-26.json；补充执行方案在 docs/p3-followup-plan-2026-10-26.md。基于 `1b35046ba3bf03de4ab126c50634da8d8aeec40b`，本轮有未提交修改；遵照用户不自行提交/推送的约定，尚未提交。最终源码摘要见 docs/reports/p3-followup-2026-10-26/source-files.json，不能把基准或新测试误称旧标签结果。

### 已完成、验证与位置

- P2 数据库 `agent_ops_p2_seed42`：无其他连接后临时关闭准入，0003 → 0004 → 0003 → 0004 全程通过，逐条 DDL 及总耗时已记录；降级在建账号之前，关键表行数一致。P2 用户 100 万、订单 200 万、会话 1,444,788、工单 100 万、事件 100 万。迁移后恢复准入。
- P0 `agent_ops` 实际从 0001 开始，先 pg_dump 后按依赖执行 0002/0003/0004。备份 `.local/backups/agent_ops-pre-0004-20261026T131729Z.dump`，8,821 字节、600、SHA256/pg_restore --list 核验已保存。主库仍 0 用户/订单、1 工单/2 事件，旧工单归属仍 NULL；未做备份恢复演练。
- `src/agent_ops/api.py`：两个导购入口 customer 登录、SSE 发头前鉴权，去除转发 Bearer；即使设置 AGENT_MEMORY_URL 或提供 memory_id 也不调用记忆，三项状态均 not_configured。memory 本身未改接口/固定依赖。核对固定 SDK 1e2730c 及 sibling 2fae08b，都是 RS256 UUID 身份，不能把平台 HS256/bigint 身份当成对方用户。
- `src/agent_ops/seed_ownership.py` 和 scripts/backfill-p3-seed-owners.py：仅来源有标记且关联会话唯一用户的 P2 种子回填。100 万张全部可确定，错配/可补未补/无标记却补均 0。无会话、冲突、无来源标记及 NULL 的顾客不可见规则由真实 PG 夹具覆盖；staff/admin 可读 NULL 工单。主库不回填。
- 已运行 scripts/stack.py；密钥只在 .local，stack.env/application.env/p2-application.env/demo-accounts.json 权限均 600，真值扫描未进入日志/源码。P2 名单 3 顾客（关联用户 1/2/3）、2 客服、1 管理员，仅在私有名单保存密码。不要打印/提交名单或 URL。
- scripts/benchmark-b0.py、benchmark-b1.py 默认认证，建账号/登录在 warmup 和计时之前；`--auth off` 仅基准工厂 scripts/benchmark_app.py 使用。常规 API 工厂不读此开关。四组 × 五轮 × 3840 合约检查全通过，分别记录认证状态/源码摘要；存在同机其他负载，只当脚本合约烟雾，不作性能提升或跨日期对比。baseline-v0/b0/b1 标签和旧结果未修改。
- 最终 pytest 135 passed / 0 skipped / 55.787 秒（2 个既有 JUnit 警告），ruff 通过，strict mypy 21 文件通过；固定规则回归 47/47，不当真实准确率。一次性 PG 双进程 TCP 26/26，P2 真实库双进程 TCP 37/37（只写认证会话，无新增订单/工单），临时 API 都已停止。原件 `.local/p3-followup-2026-10-26/`，公开报告 docs/reports/p3-followup-2026-10-26/。

### 快速上手与关键文件

- 环境：Python 3.12、uv lock、PostgreSQL 16；密码 Argon2id、JWT HS256。`bash scripts/bootstrap.sh` 安装。`uv run ruff check src tests`；`uv run mypy --strict src`；`AGENT_MEMORY_SOURCE=../agent-memory/.local/compat-1e2730c uv run pytest -q`；`uv run python -m agent_ops.evals.support_runner`。memory 源码必须匹配固定 SDK、src/migrations 干净。
- 默认主库运行：`uv run python scripts/serve.py --backend postgres --port 8003`。P2 演示：`uv run python scripts/serve.py --backend postgres --env-file .local/p2-application.env --port 8003`。绑定 localhost，默认配置未切到 P2，不自动常驻/公网部署。登录使用私有名单，本轮未在空主库创建演示顾客。
- `src/agent_ops/auth.py` / auth_postgres.py：密码、访问/刷新会话与身份；access_api.py / support_api.py：角色和用户归属；commerce_backend.py：PG 订单/会话；seed_ownership.py：一次性种子维护规则。migrations/env.py 支持外部连接，维护可在禁新连接时使用已建立连接执行迁移；0004 逐语句执行以记录 DDL 时间。scripts/apply-p3-databases.py 为本轮维护入口，已有完整报告时拒绝重复执行，不当日常栈启动替代。
- 当前验证用 scripts/verify-p3-http.py（一次性数据库）或 verify-p3-persistent.py（P2，写认证会话）；旧 verify-p1-http.py 仍限历史标签，不能在当前认证接口上沿用共享人工令牌。

### 已知问题、返工与下一步

1. 本条原为 P3 等待验收状态，现已获 P4 授权；当前进度以下方 2026-10-27 P4 节为准。客服直接订单访问仍 403，只在工单内查看本单关联订单并逐次审计。
2. P7 前缺口：双方确定服务身份、平台用户 ID 映射与权限协议；当前继续 not_configured。如需改 agent-memory 接口，必须先汇报、批准后同步两边文档，不直接在 sibling 仓库改 API。
3. 原迁移 SQL 注释有分号，拆语句计时首次误分导致事务回滚，已修正并验证；主库未到 commerce 的检查假设已按实际 0001 起点修正。不要只按旧报告猜迁移版本。
4. 种子 UPDATE 已提交后的初版只读核验为低效关联查询，已取消且改集合 JOIN 只读复核；没有重复更新。原 UPDATE 耗时没落盘，记录 null，不估算；单条加列/索引计时均完整。本轮实际回填记录见 ownership 字段；脚本已改提交后立即保存数量/耗时。
5. 维护脚本异常时 finally 恢复连接准入；若被强制终止，先由管理员核对 pg_database.datallowconn，确认库无别人在维护再恢复，不能盲目杀其他会话。
6. 未做 P4+、登录限流、前端、支付、优化实验、真实模型调用、完整恢复演练、常驻和公网部署。本轮不需要额外技术决策，等待 P3 验收。

### 飞书归档

沿用已确认「我的文档库 / WorkPlan / agent-ops-platform：多 Agent 编排与客服处理平台」（项目说明兼项目概览：https://kcn0pbn0hwo0.feishu.cn/wiki/SexSwyRWAi765SksumxcSfbKnfe）。使用 codex profile、已核验 luochen 用户身份、显式 --as user，不建新目录。既有 B1：https://kcn0pbn0hwo0.feishu.cn/wiki/HrEXwOOKRi7jrbkMf9ocTKj5n1n；P2：https://kcn0pbn0hwo0.feishu.cn/wiki/SlNOw5pF5iUYTJk960Qc788inKf。

开发记录：https://kcn0pbn0hwo0.feishu.cn/wiki/Er1cwBohMigLPEkwozDc9rSKnEf（《agent-ops-platform 开发记录》）。2026-10-26 已以 codex profile / --as user 发布本轮范围、改动、证据、未提交版本、返工与 P7 缺口；父节点与正文回读核验通过，见 docs/reports/p3-followup-2026-10-26/publication.json。今后在此文末追加，保留首创来源和历史正文。

### 更新日志

- 2026-10-26：Codex 完成上述 P3 收尾、真实库维护及验证，更新本交接/认证文档/源码证据；CLAUDE.md 仅引用本文件。P4 等待验收。

## 2026-10-26 P3 验收与 P4 授权

负责人消息 om_x100b62cb07c708acce85125f2d6e7ab 已验收 P3，明确授权将全部 P3 改动提交推送，然后只做 P4。此前“不提交/等待 P3 验收/不进入 P4”均为历史阶段状态，由本条更新。P4 按本轮用户约束执行：单条条件更新加版本号领取、偏移量列表、不提前做 S1；两步领取仅实验入口，部分索引/游标/SKIP LOCKED 留 P10；10 分钟续期租约、持久序号 WebSocket 补发、真实角色新消息、ECD source 保持原样；P6 才退款，P5 前停止。


## 2026-10-27 P4 客服闭环（Codex）

P3 全部改动已按负责人第一步要求提交推送 main：`89009744cb5772203a64a0acb3bb05c4a321b7c3`（实际提交时间北京时间 2026-10-27 08:47；此前授权段沿用了会话环境日期 2026-10-26，执行日期以本条与 Git 时间为准）。P4 本轮实现基于此提交的工作区修改，P4 尚未提交推送，完成后等待验收，不进入 P5。

已完成：src/agent_ops/workbench.py / workbench_api.py、0005_workbench 迁移及真实 PG/TCP WebSocket 测试。P0/P2 已在无其他连接、备份后从 0004 升级至 0005，两库准入已恢复；关键业务行数和 P2 1000 万 sender=source 消息不变。备份与维护记录 `.local/p4-2026-10-27/databases.json`。最终 pytest 163 passed / 0 skipped / 65.867 秒；ruff、strict mypy 23 文件、规则回归 47/47 通过。15 项 P4 真实 PG/TCP 用例包括两个 uvicorn OS 进程的 WS 创建→领取→断线→回复/解决→跨进程补发、审计版本连续、后台任务自动回收与 SLA、领取后补单号保留租约、迁移往返/claimed 降级拒绝。兼容 TCP 27/27、真实 P2 只读联调与 WS 41/41。设计和运行约定见 docs/p4-support.md；最终公开证据 docs/reports/p4-2026-10-27/，私有原件 .local/p4-*。

实现边界：单条 CAS 领取、10 分钟租约及自动续期/回收、SLA 警告与到期、退货 waiting_human、缺物流订单 needs_information、真实订单归属查询、工单内订单查看每次审计；持久全局序号 WebSocket 在两个 OS 进程间按 after 补发。产品 P4 队列/消息/事件/顾客工单列表 OFFSET，既有 P3 订单等游标接口保留。未增加 S1 队列部分索引/游标/SKIP LOCKED，原 P3 顾客归属索引保留；危险两步领取只在 scripts/experiments/s1-naive-claim.py，必须显式开关且库名 agent_ops_s1_*，产品不导入。P5+、退款/审批资金动作和 S1 测量未做。


### P4 运行、已知限制及下一步

- 数据流：support/requests 仍是顾客入口，PG 默认 WorkbenchService；workbench_api 提供领取/回复/退回/解决、详情/工单内订单和 /ws；workbench.py 在同一事务写 tickets/messages/operations/ticket_events/push_events。push_clock 事务行锁保证可见序号按提交顺序，客户端 last_processed/after 严格补发；两个进程轮询同一 PG，未来 Pub/Sub 只唤醒，不代替持久历史。sender=source 的种子只读，不猜角色。
- 业务写带 Idempotency-Key 和 expected_revision。产品 claim 一条 state/revision 条件 UPDATE，claimed 仅持有者未过期可写。领取者查看详情/消息/审计/订单也续租及增版本，前端 P5 必须采用返回 ticket_revision。顾客补缺订单可在 claimed 中进行但不续租、不改领取者；订单已关联不得替换。退货永远 waiting_human，P4 不提供资金批准/退款。
- 新队列/消息/事件/PG 顾客工单列表 OFFSET；已有 P3 订单等游标/归属索引仅保留，不当 S1 优化。危险两步基线脚本必须 --enable-unsafe-s1-baseline 且 AGENT_OPS_S1_DATABASE_URL 库名 agent_ops_s1_*，安全门已测拒绝正常库；S1 领取实验本身未运行。
- 实库备份（均 600、目录可读；完整恢复未验证）：P0 `.local/backups/agent_ops-pre-0005-20261027T010353Z.dump` 61,157 字节；P2 `.local/backups/agent_ops_p2_seed42-pre-0005-20261027T010401Z.dump` 301,169,049 字节。迁移约 0.090/5.229 秒，行数和 1000 万 source 消息角色不变。历史合成工单 SLA 从迁移时刻 normal +24h 初始化，不作为历史真实 SLA；旧订单关联不猜测回填。
- 启动：uv sync；`uv run python scripts/serve.py --backend postgres --env-file .local/p2-application.env --port 8003`，仅 localhost，未常驻。真实库只读烟雾：`uv run python scripts/verify-p3-persistent.py --output-dir .local/p4-new-smoke --workbench`（只新增认证会话，输出路径必须新建）。完整写入闭环用一次性 PG 测试，不污染百万库。新增 websockets 锁定 16.1.1，uv.lock 是版本依据。
- 后台每秒批量条件检查，多个进程避免重复释放/SLA事件；大规模到期吞吐、提交序号门性能、WS 容量、完整备份恢复均未验证。没有清理持久推送历史，未来保留期限须配套游标过期协议。0005 降级会移除 P4 数据，先备份并释放 claimed，测试库往返通过，服务器未降级。
- 已知失败已修：两项旧 PG HTTP 用例跳过领取被拒绝，改为 claim 后 resolve 且带幂等键，保留权限/归属/审计断言；补齐 mypy 查询类型、异步测试子进程启动和显式 check、后台测试缺失导入。最终四道门通过，无进行中的迁移/API进程。P4 原始较早 162 项日志留作返工历史，不替代最终 163 项。
- 下一步：等负责人验收 P4；本轮 P4 工作区修改未提交推送，不自行进入 P5/P6/P10。无新技术决策待定。P7 memory 服务身份协议缺口仍 not_configured，若改接口先报告再同步双方文档。
- 飞书已同步：2026-10-27 P4 交付和完整接口/运行说明已追加既有《agent-ops-platform 开发记录》：https://kcn0pbn0hwo0.feishu.cn/wiki/Er1cwBohMigLPEkwozDc9rSKnEf。父节点/正文回读通过，确认 P3 正文完整保留（导出标题标记归一后核对）；用户身份 codex profile / --as user。核验见 docs/reports/p4-2026-10-27/publication.json。

### 更新日志（P4）

- 2026-10-27：Codex 完成 P3 提交推送及上述 P4 实现、两库备份迁移、163 项回归和真实双进程 WS 验证；P5 等待 P4 验收。

## 2026-10-27 P4 验收与 P5 授权

负责人消息 om_x100b61333a019caccf035b587c1094e 已验收 P4，要求先提交推送全部 P4 改动，再只做 P5 React/TypeScript 前端。此前等待 P4 验收/不进入 P5 为历史阶段状态，由本条更新。P5 只绑定回环，SSH 转发访问；支付与 P7 依赖部分明确“尚未开通”，不冒充真实能力；Playwright 主流程/双标签隔离/私有截图、前后端检查后停在 P6 前。Codex 本轮独立主导执行，适用独立任务安排。
