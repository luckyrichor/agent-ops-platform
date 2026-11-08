# 指定存储范围可恢复性与服务关停核查（2026-11-08）

范围：仅核查用户指定的agent-ops-platform数据集/素材、依赖/构建/缓存、报告/评测/实验记录、数据库备份/本地持久化，以及backend-cloud-labs的Git历史、报告/记录和.local/bin；另核查四项目数据卷和bench-pg来源。本轮没有删除上述任何数据。CodeBuddy执行授权关停，Codex亲自验收并审计恢复条件。

## 已完成关停

4个user服务：agent-ops-p7-web、agent-ops-p7-api、agent-ops-p6-sandbox-api、agent-ops-customer-memory。全部MainPID=0，监听端口已关闭；web收SIGTERM后systemd保留failed状态，表示已退出而非仍在运行，未reset-failed、disable或修改重启配置。

8容器全部Exited(0)：agent-ops-tracing-{grafana,prometheus,collector,jaeger,redis,postgres}-1、agent-ops-p7-memory、s1-postgres。逐一docker inspect核实停止，原挂载源仍存在，未删除容器或卷、未修改restart policy。bench-pg按“查来源”范围保留运行；其他服务和日志轮转timer未修改。私有机器审计记录：.agent-tasks/2026-11-08-stop-services-result.json。

## 判断标准

- 可恢复：已提交GitHub的文件、可按锁文件重装依赖、由保留输入确定性生成的派生物。重建不等于可以恢复历史执行时的原始返回/数据库状态。
- 条件可删：须先留住清单、生成输入、版本和校验信息；本轮不实际删除、重装或重跑全量数据。
- 不直接删：本地唯一记录、人工标注、模型原始响应、未导出的数据库/监控历史，以及未核实恢复链的归档。

## agent-ops-platform

| 内容 | 当前占用 | 可恢复性判断 |
|---|---:|---|
| .venv、frontend/node_modules | 299.4MiB | 可删除后按uv.lock/package-lock重装；代码、锁文件、私有配置另留。未实际重装验证，不承诺外部依赖永久可取 |
| 检查缓存、__pycache__、frontend/dist | 约22.9MiB | 可重新检查/构建；首次重建更慢 |
| 4套scale42 CSV | 合计约6.25GiB | 条件可删。scripts/synthesize-p2.py等已有确定性生成入口，seed42；先保留各批manifest/校验、生成代码版本、fit-v1及原始源包。不能说删后直接重新下载CSV；本轮未全量重建验证逐字节相同 |
| userbehavior-v1的大JSONL/派生CSV | 目录共313.2MiB，含需保留的清单 | 大文件可列条件候选；verified-fit、events/batch manifests、源校验和生成脚本不能整目录删，依赖冻结base批次，恢复需重跑 |
| fit-v1 | 28.1MiB | 保留。含fits.json、olist-bootstrap.json、ecd-bootstrap-local.json，是派生数据重建输入 |
| raw三原始包 | 约1000MiB | 保留。Olist现有脚本URL本次HEAD与小范围GET均404；ECD URL HEAD200但未重下全包验相同hash；UserBehavior页面200不等于可免登录下载相同归档，原脚本明确需要owner提供源文件 |
| docs内已提交报告 | 文件块约7.0MiB | 可从当前GitHub提交恢复。本轮Git对象对照通过；两CSV原始字节的CRLF/LF差异经git hash-object过滤后与index一致，不是独有修改 |
| .local内实验请求/返回/记录 | 主要约356MiB，另有其他小记录 | 不直接删。P8开发、release/recovery中有模型请求/返回与调用缓存，P9有逐次性能原始样本；汇总报告不是这些逐次记录的完整备份，模型重跑有成本且不保证相同结果 |
| 两主要dump及小备份 | 约574.9MiB | 保留。不同历史时间点的本地PG备份，不能从Git恢复；未做隔离恢复和内容去重核验，不把它们当重复文件删 |

## backend-cloud-labs

- .local/bin约71.6MiB：e1/e2/e3/e5、loadgen、loadgen-register、rule-eval均有仓库构建入口（scripts/build-deploy.sh及其他脚本），属于可重编译产物。未实际重编译验证；保留源码、Go模块锁定和工具链约定。
- .git约466.8MiB：当前main=e12c703与GitHub一致；未发现其他本地分支、stash、额外worktree、tags或仅本地branch提交。重新clone可恢复已发布代码和历史，但不能恢复本地reflog、info/exclude及未发布/不可达对象；本轮没有把不可达对象等同无用垃圾。不要直接删除.git让现目录脱离版本管理；若要省空间，可另行评估新clone体积并保留本机排除规则。不能保证重clone会减少同样空间。
- docs/measurements文件块约512.4MiB已Git跟踪，本地内容与Git对象一致（完整目录约514MiB还含目录块）。可从GitHub恢复；本地删后会显示未提交删除，不能将“本机释放空间”误操作成向GitHub提交删除报告。
- .local/s1-single等约四十多MiB原始测量/中间记录，未在Git中；本轮未证明均有逐文件等价备份。可重跑实验不等于复得当时的原始延迟、环境与事件，保留。

## 数据卷核查

不启动已停容器、不删除卷、不重建数据库。PG卷通过前轮在线只读目录清单/库大小与本轮卷文件结构交叉核查；已停库未重新启动导出，本次不声称完成逐表/逐行恢复验证。

| 卷组 | 占用约 | 内容与建议 |
|---|---:|---|
| agent-ops tracing PostgreSQL | 6.26GiB | 停前列出agent_ops_p2_seed42约4.92GiB逻辑库、bench/s1、沙箱及agent_ops等库。合成种子可重建不等于付款/退款/审计或实验后的DB状态可恢复；没有完整匹配导出证据，保留 |
| s1-postgres | 878.8MiB | agent_ops_p10_s1与s2_template，历史数据库/模板。可重新建基础环境，历史变更恢复链未核验，保留 |
| agent-ops-p7-memory | 46.4MiB | customer_memory的PG目录；用户记忆/幂等/审计状态可能唯一，保留 |
| agent-ops Redis | 约20KiB | 持久化文件很小，未核对全部key语义，不贸然视为纯可弃缓存 |
| 两项目Prometheus、Jaeger | 合计约48MiB | 历史时序与trace不能重下载当时值。需先确认重要历史已导出才可弃，当前保留 |
| 两项目Grafana | 合计约25.7MiB | sqlite只读查到各0个dashboard、1个datasource；基础面板/数据源有代码可重建，但用户/偏好等本地状态不保证完整恢复，导出配置后可列条件候选 |
| backend MySQL | 约2.80GiB | MySQL表空间、redo/binlog等，初始化脚本有bcl/bench_run/p0_binlog。初始表可重建，历史binlog/写入状态不能由Git恢复；未启动核验逐表数据，保留 |
| backend Redis卷及.local/s2/redisdata | 卷很小，bind约13.9MiB | 项目使用缓存也使用Redis Stream和checkpoint，不能无条件称纯缓存；未核对全部历史key/导出，保留 |
| backend E5 cmdb_data | 空目录，约4KiB目录块 | 内容为空，属于可删除候选；将来启动能重建目录，无明显空间收益 |
| agent-memory原PG卷 | 46.4MiB | 本地旧库，未完成恢复/唯一性核验，保留 |
| agent-memory两diskfull测试卷 | 合计约351.2MiB | 磁盘满实验的PG现场。不是普通可下载数据；脚本可再造故障不等于再得原现场/原WAL，确认不保留现场或导出后才可删除 |
| engine-core-labs | 未发现关联Docker卷 | 无本轮卷删除候选 |

## bench-pg来源

容器元数据Created=2026-11-03T06:00:57.873075808Z（北京时间14:00:57），镜像pgvector/pgvector:pg16，restart=no，宿主55432映射。容器没有compose归属labels；数据卷约1.08GiB。

在线只读核查：bench逻辑库约361.9MiB，benchc4约16.7MiB。均包含memories、memory_versions、memory_embeddings、jobs、audit_logs、outbox_messages等记忆服务表；alembic_version为0013_job_priority。bench含24,000 memories、24,000 embeddings、24,000 jobs；benchc4含1,505 memories、0 embeddings、3,010 jobs。它与agent-memory的数据模型一致，可判断为记忆服务相关实验库，不是导购commerce数据库。数据库内未读取记忆正文。

**未定位精确创建命令或创建者**：现有项目清理记录曾把它当其他项目保留，不能用这条旧备注确定归属。数据库基准性质是根据表结构/规模推断，不等于已证明其中所有数据都可丢弃。当前仅查来源，未获关停/删除指令，故保留运行与卷；不把“有benchmark字样”视为恢复保证。

## 结论

可优先列删除候选：agent-ops依赖/构建缓存约322MiB、backend可重编译bin约72MiB；Git已保存的报告可本地移除后按提交恢复。约6.25GiB的4套派生CSV需先固化恢复输入/版本/清单，属于条件可删。本地原始响应/人工结果/测量、原始数据包、fit输入、历史dump和未核实数据库卷不直接删。所有删除均未执行。
