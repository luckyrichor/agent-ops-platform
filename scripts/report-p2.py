"""Generate P2 handoff from actual archives, scale manifests, DB results and gate logs."""
import hashlib,json,re,shutil,subprocess
from datetime import datetime,UTC
from pathlib import Path
from zoneinfo import ZoneInfo
root=Path('.local/datasets');out=Path('docs/reports/p2-2026-10-21');out.mkdir(parents=True,exist_ok=True)
def read(path):return json.loads(Path(path).read_text())
def write(path,v):Path(path).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
v=read(root/'verified.json');fit=read(root/'fit-v1/fits.json');data=read(root/'scale42-proxy05-a/manifest.json')
repro=read(root/'repeat-proxy05-proof.json');db=read(root/'import-proxy05.json');verify=read(root/'db-verification.json');speaker=read(root/'speaker-check.json')
assert db['complete'] and repro['all_files_identical']
assert db['source_manifest_sha256']==hashlib.sha256((root/'scale42-proxy05-a/manifest.json').read_bytes()).hexdigest()
log=Path('.local/p2-final-pytest.log').read_text();test=re.search(r'(\d+) passed.*?in ([\d.]+)s',log)
assert test and 'failed' not in log and 'skipped' not in log
for name in ['ruff','mypy']:
 text=Path('.local/p2-final-'+name+'.log').read_text();assert ('All checks passed!' if name=='ruff' else 'Success: no issues found') in text
plan=read('docs/plans/source-2026-10-21.json');source=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
for tag in ['baseline-v0','baseline-b0','baseline-b1']:
 subprocess.run(['git','rev-parse','--verify',tag],check=True,capture_output=True)
checks={table.rsplit('.',1)[-1]:n for table,n in verify['counts'].items()}
targets={'products':200000,'users':1000000,'orders':2000000,'tickets':1000000,'messages':10000000}
assert all(checks[k]>=n for k,n in targets.items())
# Proxy weights must never change the fixed regression labels.
assert subprocess.check_output(['git','show','baseline-b1:src/agent_ops/evals/support_runner.py'])==Path('src/agent_ops/evals/support_runner.py').read_bytes()
stats=[]
for name,d in v['datasets'].items():
 for item in d.get('members',[]):
  if 'rows' in item:stats.append(f"| {name} | {item['name']} | {item['rows']:,} | `{item['sha256']}` |")
 downloads={}
for name in ['olist','ecd']:
 text=(root/(name+'-download.log')).read_text();m=re.search(r'http=(\d+) bytes=(\d+) seconds=([\d.]+)',text)
 assert m;downloads[name]={'http_status':int(m[1]),'downloaded_bytes':int(m[2]),'seconds':float(m[3])}
 diff=lambda actual,reference:max(abs(actual.get(k,0)/sum(actual.values())-reference.get(k,0)/sum(reference.values())) for k in set(actual)|set(reference))
errors={'category_max_absolute_share_diff':diff(data['generated_distributions']['category_counts'],fit['olist']['product_categories']),
        'order_status_max_absolute_share_diff':diff(data['generated_distributions']['order_status'],fit['olist']['order_status']),
        'conversation_length_max_absolute_share_diff':diff(data['generated_distributions']['conversation_turns'],fit['ecd']['turn_count_histogram'])}
# JSON histogram keys are strings in both artifacts; no schema changes or manual numbers.
r={'generated_at':datetime.now(UTC).isoformat(),'date_beijing':str(datetime.now(ZoneInfo('Asia/Shanghai')).date()),
 'source_commit':source,'source_dirty':bool(subprocess.check_output(['git','diff','--','src','migrations','scripts'])),
 'plan':plan,'scope':'P2 only; B1 separately reported; no P3 or later stages',
 'datasets':v,'download_evidence':downloads,'fits':fit,'generated':data,'repeat_proof':repro,'import':db,'database_verification':verify,
 'speaker_quick_check':speaker,'fit_deviations':errors,'targets':targets,'actual_target_rows':checks,
 'gates':{'pytest_passed':int(test[1]),'seconds':float(test[2]),'skipped':0,'ruff':True,'strict_mypy':True,
          'regression_only':read('.local/p2-final-eval.json')},
 'disk':{'datasets_workdir_bytes':int(subprocess.check_output(['du','-sb',str(root)],text=True).split()[0]),
         'database_bytes':db['database_bytes'],'final_csv_bytes_per_copy':data['total_csv_bytes'],
         'filesystem_free_bytes':shutil.disk_usage('.').free},
 'intent_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认',
 'waiting_human_ratio_configurable':True,'s1_experiments_executed':False,'replace_proxy_after_P8':True,
 'scale_counts_pass':True,'determinism_pass':True,'import_measured':True,'all_datasets_available':False,
 'p2_accepted':False,'model_calls':0,'p3_or_later_started':False,
 'remaining':['UserBehavior owner-downloaded full ZIP missing; actual file/row/hash/fit and behavior events unverified',
              'user activity NULL and order-user linkage is structural placeholder, not UserBehavior distribution',
              'P8 human intent labels not done; proxy weight replacement deferred to P8',
              'main HTTP app not switched to P2 scale database; future commerce/support integration belongs to P4/P7'],
 'failures_and_rework':['initial admin username postgres was wrong for existing compose; fixed to configured ops_owner without credential change',
                      'mypy caught local Row variable reuse; corrected before final gates',
                      'ruff caught missing explicit check=False in expected-failure test; corrected before final gates',
                      'owner approved proxy during base import; preserved base data and augmented only proxy tables and conversation linkage after hash checks',
                      'partition sum size returned Decimal and broke JSON summary after load committed; fixed cast and recovered counts/sizes read-only; initial ANALYZE wall time unrecorded'],
 'original_data_exported':False}
write(out/'report.json',r)
for src,name in [(root/'verified.json','dataset-verification.json'),(root/'fit-v1/fits.json','fits.json'),
 (root/'repeat-proxy05-proof.json','repeat-proof.json'),(root/'import-proxy05.json','import.json'),
 (root/'db-verification.json','database-verification.json'),(root/'speaker-check.json','speaker-check.json'),
 (Path('.local/p2-final-pytest.log'),'pytest.log'),(Path('.local/p2-final-gates.xml'),'pytest.xml'),
 (Path('.local/p2-final-ruff.log'),'ruff.log'),(Path('.local/p2-final-mypy.log'),'mypy.log')]:shutil.copyfile(src,out/name)
# No source dialogue, fitted raw bootstrap samples or synthetic reused ECD text in public files.
for name in ['olist','ecd']:
 d=v['datasets'][name];assert d['crc_pass'] and d['archive_bytes']==d['expected_bytes']
source_lines=[]
for name in ['olist','ecd']:
 d=v['datasets'][name];source_lines.append(f"| {name} | {d['archive_bytes']:,} | `{d['archive_sha256']}` | {downloads[name]['http_status']} | {downloads[name]['seconds']:.3f} 秒 |")
Path('docs/datasets.md').write_text(f'''# P2 数据集核验与来源

由 Codex 生成 · {r['date_beijing']} · agent-ops-platform@{source[:12]}。完整记录由 `scripts/report-p2.py` 自动生成，见 [原始统计](reports/p2-2026-10-21/dataset-verification.json)。所有原文与复用 ECD 原文的合成数据仅在 tx `.local/datasets/`，不进仓库、不发群、不再分发。

## 完整下载与校验

| 数据集 | ZIP 实际字节 | SHA256 | HTTP | 下载耗时 |
| --- | --- | --- | --- | --- |
{chr(10).join(source_lines)}

两份 ZIP 全部成员 CRC 通过，大小与方案一致。CSV 采用 CSV 解析计数并排除表头；ECD 按非空行计数，不拿物理换行数冒充带引号 CSV 的行数。

| 数据集 | 文件 | 实际记录数 | 成员 SHA256 |
| --- | --- | --- | --- |
{chr(10).join(stats)}

UserBehavior：**需负责人提供登录后下载的完整版文件**。预期 ZIP 大小 {v['datasets']['userbehavior']['expected_archive_bytes']:,} 字节（方案元数据，尚非实测）；本轮实际获得数据字节 {v['datasets']['userbehavior']['downloaded_bytes']}，实际行数/SHA256/Zipf 指数均未验证。只读取公开页，未登录、未注册、未使用镜像替代，不要求在对话中发送密码或令牌。

## 下载地址与许可

- [Olist 官方页面](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，按方案固定版本直链下载；CC BY-NC-SA 4.0，非商业、署名及相同方式共享约束按原许可处理。
- [ECD 作者仓库](https://github.com/cooelf/DeepUtteranceAggregation)，使用其原 Google Drive 文件。未发现明确数据许可；仅本地个人评测，公开材料只有方法、哈希和统计，不再分发文本。引用：Zhang 等，COLING 2018，Modeling Multi-turn Conversation with Deep Utterance Aggregation。
- [UserBehavior 天池页面](https://tianchi.aliyun.com/dataset/649)；许可 CC BY-NC-SA 4.0 目前仅按方案记录，原始文件仍未获得。

## 分布与局限

Olist 拟合联合经验分布并重采样：商品类目与有观测的价格/运费，订单状态/时间/原购物篮组合。无价格商品 {fit['olist']['products_without_price']} 个未补假价格。源月份实测从 {min(fit['olist']['order_month'])} 至 {max(fit['olist']['order_month'])}，方案的日期描述不是完整范围；源时区未提供，合成视为 UTC，不声称真实绝对时间。商品标题、库存、物流跟踪号为明确的合成字段。

ECD 去重正例上文 {fit['ecd']['unique_positive_contexts']:,} 段，负响应候选不当作真实对话续句；空格按分词还原要求移除。实测片段数从 {min(map(int,fit['ecd']['turn_count_histogram']))} 至 {max(map(int,fit['ecd']['turn_count_histogram']))}，与 README 概括范围不同，未偷偷截断。首片段说话人快速线索检查 {speaker['sample_n']} 条：{speaker['cue_counts']}，不是人工核验，无法据此确认身份。

**规则代理，非人工真实意图分布，首片段说话人未确认。** 经负责人批准，现有规则首片段命中权重只用于合成类别占比。全量工单行、事件载荷、manifest 与每文件 provenance 都保留此标记；不得用于路由准确率或质量指标。等待人工占比独立参数，当前 {data['waiting_human_ratio']:.0%}；未来 S1 做多档实验，本轮未做。P8 人工标注完成后替换并记 progress。

## 重建

```bash
uv run python scripts/download-datasets.py
uv run python scripts/fit-p2-data.py --verified .local/datasets/verified.json --output .local/fit-new
uv run python scripts/synthesize-p2.py --fit .local/fit-new --output .local/scale-new --seed 42 --owner-approved-ecd-proxy --waiting-human-ratio 0.05
```

UserBehavior 到位后显式传 `--userbehavior-file .local/datasets/raw/UserBehavior.csv.zip` 再核验，重新拟合并建新批次；当前未用猜测/均匀分布冒充其活跃度或商品热度。
''')
rows=[f"| {k} | {n:,} | {checks[k]:,} |" for k,n in targets.items()]
loadrows=[f"| {t['table']} | {t['rows']:,} | {t['table_and_indexes_bytes']:,} |" for t in db['tables']]
(out/'delivery.md').write_text(f'''# agent-ops-platform P2 交付与缺口

由 Codex 生成 · {r['date_beijing']}（北京时间）· agent-ops-platform@{source[:12]} · 未提交源码修改：{r['source_dirty']}。所有数字由 `scripts/report-p2.py` 从实际文件、数据库和测试日志生成。原始数据未入仓库。**仅 P2；B1 已先单独交付并打 baseline-b1；P3 及以后未做。**

## 做了什么

商品/类目、库存、订单/明细、物流、用户标识、会话与月分区消息迁移；本地应用服务和 CLI。幂等下单与库存预留同事务、失败回滚、取消释放、版本并发校验；消息注册表保证跨分区序号唯一。使用原 P0 PostgreSQL 实例内的隔离验收数据库 `{db['database']}`，未清空旧业务数据库，也未把本地可信 user_id 过滤称为登录鉴权。HTTP 客户认证留 P3，客服/导购实际接入留后续阶段。

完整下载、CRC、大小、行数和 SHA256 见 [docs/datasets.md](../../datasets.md)。Olist/ECD 到位；UserBehavior 缺文件，未登录或注册。商品和订单做联合经验重采样，ECD 做正例去重及长度分层采样，随机种子 {data['seed']}。

## 行数、校验和与实际导入证据

| 实体 | 方案目标 | 实际数据库行数 |
| --- | --- | --- |
{chr(10).join(rows)}

两次完整生成全部 CSV 与 provenance 校验和一致（输出路径、耗时等运行元数据不纳入数据校验和），集合校验和 `{repro['dataset_checksum']}`；两次生成耗时 {repro['generation_seconds'][0]:.2f}/{repro['generation_seconds'][1]:.2f} 秒。见 [逐文件校验](repeat-proof.json)。

数据导入累计 {db['total_data_import_seconds']:.2f} 秒（基础表实际保留的 COPY/计数耗时，加批准后的代理工单/事件及关联补充；首次 ANALYZE 耗时未记录，不作为精确总墙钟）；分阶段、分表实测见 [import.json](import.json)。基础表 SHA256 与新批次核对一致，未把不同数据悄悄混成一次导入。导入期间同机存在常驻服务及生成/测试负载；这只是本机导入耗时记录，不是性能优化结果。

单份最终 CSV {data['total_csv_bytes']:,} 字节；数据库（含索引）{db['database_bytes']:,} 字节；完整数据工作目录含下载、失败/中间批次与两份正式生成，占 {r['disk']['datasets_workdir_bytes']:,} 字节。该目录占用不是单份 CSV 体积，也不含 Docker 数据卷，不能相加时重复算数据库。

| 数据表 | 行数 | 表与索引字节（分区汇总） |
| --- | --- | --- |
{chr(10).join(loadrows)}

全量只读校验：订单总额不一致 {verify['order_total_mismatch']}，库存约束违反 {verify['inventory_check_violations']}，未验证约束 {verify['unvalidated_constraints']}，缺代理标记工单 {verify['proxy_note_missing_rows']}，会话工单关联错误 {verify['conversation_ticket_link_mismatch']}。见[数据库证据](database-verification.json)。

## 代理条件与方案出入

**规则代理，非人工真实意图分布，首片段说话人未确认。** 只用于合成类别占比，不进入任何路由准确率或评测真值；既有固定回归数据与运行器未改。等待人工独立配置为 {data['waiting_human_ratio']:.0%}、实际 {verify['waiting_human']:,}/{verify['ticket_count']:,}，不从代理意图比例固定推导。参数生成测试覆盖多档值；后续 S1 稳定性实验未做，P8 人工标注后替换代理的要求已写入策略和 progress。

- UserBehavior 文件未获得，实际行数/哈希/活跃度/Zipf 与浏览加购事件未验证。用户行只是标识占位，activity_events 留空；订单与用户作确定性结构关联，不能当作行为分布。脚本遇缺拟合会拒绝生成访问事件，不猜指数或换源。
- ECD 无显式说话人角色；快速线索探测 {speaker['sample_n']} 条仍不能确认身份。消息 sender 保留 source，不编造 customer/agent；这是来源限制，不把它包装成已完成人工标注。
- Olist 实测含方案未提及的较早月份；无观测价格商品不补零假装真实价格。商品标题、库存、物流号及队列状态均为明确合成；等待人工以外工单状态是负载占位，不是客服流程质量证据。
- PG 分区表不能单靠 (conversation_id, seq) 唯一约束跨月去重，增设 message_keys，增加磁盘和写入成本，已实测记录。
- 当前是独立验收数据库，本地后端可查询；主 HTTP 应用尚未切库。未提前实施认证、工单闭环、缓存、支付、模型网关或 S1–S7 优化。

## 测试、失败与返工

pytest {r['gates']['pytest_passed']} passed，{r['gates']['seconds']} 秒，跳过 {r['gates']['skipped']}；ruff、strict mypy、固定回归运行器通过。真实 Testcontainers 验证事务、并发、回滚与迁移；原始日志在本目录。首次错误管理员名造成连接失败，核对既有配置后修正；静态类型和预期失败测试的 lint 问题已修正。分区体积汇总的 Decimal 曾导致 JSON 序列化失败；数据已提交，修复后只读恢复计数/磁盘统计，没有重导或修改原始耗时；首次 ANALYZE 耗时未记录。负责人批准代理期间已在进行基础导入，因此保留中间证据并按哈希核对后补充代理部分，未重新标注或伪称单次完成。

## 未做与需要负责人提供的内容

**P2 尚未全部验收：缺 UserBehavior 完整版。** 请把登录下载的 `UserBehavior.csv.zip` 放到服务器 `.local/datasets/raw/`，或通过私有渠道提供文件；不需要在群里发密码/令牌。文件到位后才能完成全 ZIP 行数/哈希核验、活跃度与热度拟合，并重建依赖该分布的批次。ECD 代理选择已获批准，无重复待确认项。P8 权重替换和 S1 多档实验均未提前做。

复现与证据路径：代码脚本位于 scripts/，原始/拟合/生成文件仅服务器 `.local/datasets/`；下载与拟合命令见 datasets.md。独立完整导入可用 `uv run python scripts/import-p2.py --data .local/scale-new --database agent_ops_p2_new --output .local/import-new.json`，拒绝覆盖既有数据库。额外来源与设计取舍见 docs/design-decisions.md；本轮未调用真实模型。可另行整理为飞书文档，目标目录建议 agent-ops-platform/技术文档；确认标题、内容范围及使用身份后再写入。
''')
print(json.dumps({'report':str(out/'delivery.md'),'p2_accepted':False,'remaining':'UserBehavior owner file missing'}))
