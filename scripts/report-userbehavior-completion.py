"""Generate P2 follow-up facts exclusively from actual verification and run artifacts."""
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT=Path('.local/datasets/userbehavior-v1')
OUT=Path('docs/reports/p2-userbehavior-2026-10-23')
OUT.mkdir(parents=True,exist_ok=True)
read=lambda p:json.loads(Path(p).read_text())
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
f=read(ROOT/'verified-fit-v2.json');a=read(ROOT/'final-a/manifest.json');b=read(ROOT/'final-b/manifest.json')
ea=read(ROOT/'events-final-a.manifest.json');eb=read(ROOT/'events-final-b.manifest.json')
db=read(ROOT/'import.json')
assert set(a['files'])==set(b['files'])
for key in a['files']:
    for prop in ['rows','bytes','sha256','provenance_sha256']:
        assert a['files'][key].get(prop)==b['files'][key].get(prop),(key,prop)
assert ea['sha256']==eb['sha256'] and ea['events']==eb['events']
for path, expected in [(ROOT/'events-final-a.jsonl',ea['sha256']),(ROOT/'events-final-b.jsonl',eb['sha256']),(ROOT/'final-a/users.csv',a['files']['users']['sha256']),(ROOT/'final-b/users.csv',b['files']['users']['sha256'])]:
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    assert digest.hexdigest()==expected
assert db['complete'] and db['unaffected_full_table_hashes_identical']
xml=ET.parse('.local/userbehavior-pytest-final.xml').getroot()
suites=list(xml.iter('testsuite'))
gates={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
assert gates['failures']==gates['errors']==gates['skipped']==0
assert 'All checks passed!' in Path('.local/userbehavior-ruff.log').read_text()
assert 'Success:' in Path('.local/userbehavior-mypy.log').read_text()
eval_=read('.local/userbehavior-eval.log');assert eval_['cases']==eval_['passed']
gates.update(ruff=True,strict_mypy=True,fixed_regression_passed=eval_['passed'],fixed_regression_is_quality_evidence=False)
plan=read('docs/plans/source-2026-10-23.json')
commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
dirty=bool(subprocess.check_output(['git','diff','--','src','tests','scripts','migrations'],text=True))
counts=db['counts']
targets={'commerce.products':200000,'commerce.users':1000000,'commerce.orders':2000000,'support.tickets':1000000,'commerce.messages':10000000}
assert all(counts[k]>=v for k,v in targets.items())
proof={'seed':a['seed'],'full_user_batch_runs':2,'events_per_run':ea['events'],'events_sha256':ea['sha256'],
       'all_data_file_checksums_identical':True,'unchanged_tables_reused_from_frozen_batch':True,
       'files':{k:{p:v.get(p) for p in ['rows','bytes','sha256','provenance_sha256']} for k,v in a['files'].items()},
       'timings_excluded_from_checksum':True}
r={'generated_at':datetime.now(UTC).isoformat(),'source_commit':commit,'source_dirty':dirty,
   'date_beijing':str(datetime.now(ZoneInfo('Asia/Shanghai')).date()),'scope':'P2 only',
   'plan':plan,'source_verification_and_fit':f,'batch_manifest_sha256':sha(ROOT/'final-a/manifest.json'),
   'repeat_proof':proof,'database_import':db,'gates':gates,
   'targets':targets,'target_counts_pass':True,'determinism_pass':True,'all_three_sources_available':True,
   'source_archive_only_private':True,'main_application_database_switched':False,
   'p3_or_later_started':False,'optimization_experiment_executed':False,'live_model_calls':0,
   'disk':{'raw_zip_bytes':f['archive_bytes'],'uncompressed_csv_bytes':f['members'][0]['uncompressed_bytes'],
           'new_batch_csv_bytes_per_run':a['new_csv_bytes'],'combined_logical_csv_bytes':a['total_csv_bytes'],
           'events_bytes_per_run':ea['bytes'],'database_bytes':db['database_bytes'],
           'workdir_bytes':int(subprocess.check_output(['du','-sb','.local/datasets'],text=True).split()[0]),
           'filesystem_free_bytes':shutil.disk_usage('.').free},
   'p2_evidence_complete':True,'owner_acceptance_recorded':False,
   'remaining':['P2 awaits owner acceptance; no P3 work',
       'P8 human intent labels and replacement of approved ECD proxy remain deferred to P8',
       'No S1-S7 experiments, A/A performance runs, cache hit-rate or throughput improvement claims',
       'Main HTTP application not switched; future commerce/support integration remains P4/P7',
       'Workload uses independently fitted marginals, not real trajectories or a conversion model'],
   'failures_and_rework':['Initial OLS log-rank diagnostic had a large probability CDF discrepancy; retained old attempts, recalibrated a finite-support probability model and regenerated both final batches',
       'Initial full-source parser rejected a source negative timestamp; corrected to retain all numeric rows and count anomalies; initial failed log retained',
       'Initial pytest: 1 failure and 92 passes because default sibling memory checkout differed from installed pinned SDK; repeated with matching detached compatibility checkout: all passed'],
   'page_metadata_revalidation':'Current public page returned only title, no file metadata; byte comparison uses exact historical Appendix C metadata, not claimed freshly obtained',
   'ecd_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认'}
for name,data in [('report.json',r),('source-verification-and-fit.json',f),('repeat-proof.json',proof),('import.json',db)]:
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
for src,dst in [('.local/userbehavior-pytest-final.log','pytest.log'),('.local/userbehavior-pytest-final.xml','pytest.xml'),('.local/userbehavior-ruff.log','ruff.log'),('.local/userbehavior-mypy.log','mypy.log')]:
    shutil.copyfile(src,OUT/dst)
# Current normalized status retains old Olist/ECD evidence, explicitly from its original date.
v=read('.local/datasets/verified.json');v['datasets']['userbehavior']=f
v['datasets']['userbehavior']['expected_bytes']=f['expected_archive_bytes']
v['status_updated_at']=r['generated_at'];v['historical_olist_ecd_reverified_this_run']=False
(ROOT/'verified-all.json').write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
behavior='\n'.join(f"| {k} | {n:,} | {f['behavior_proportions'][k]:.6%} |" for k,n in f['behavior_counts'].items())
targetrows='\n'.join(f'| {k} | {v:,} | {counts[k]:,} |' for k,v in targets.items())
rank=f['item_popularity_pv'];rankall=f['item_popularity_all_behaviors'];ols=f['item_popularity_pv_ols_diagnostic']
text=f'''# P2 UserBehavior 完整核验、分布拟合与增量重建交接

> 🤖 由 Codex 生成 · {r['date_beijing']} · 来源 agent-ops-platform@{commit[:12]} · 工具 lark-cli profile codex

代码未提交修改：{dirty}。本轮仅 P2；运行与新数据证据已齐，等待负责人验收，不进入 P3。

## 做了什么与证据

已从负责人提供的飞书附件取得完整 ZIP。实际 {f['archive_bytes']:,} 字节，与方案附录 C 的 {f['expected_archive_bytes']:,} 字节相差 {f['archive_size_difference_bytes']:,}；SHA256 `{f['archive_sha256']}`。解压成员 {f['members'][0]['name']}，{f['members'][0]['uncompressed_bytes']:,} 字节，成员 SHA256 `{f['members'][0]['sha256']}`。完整读取到 EOF，CRC 全通过，headerless CSV 五列校验通过，实际 {f['rows']:,} 行，未抽样、未去重、未截断、未静默清洗。源原文仅 tx 私有 `.local/datasets/raw/`，GitHub/飞书只放方法、哈希和聚合统计。

真实用户 ID 去重 {f['users']:,}，商品 {f['items']:,}，类目 {f['categories']:,}。源时间戳范围 {f['timestamp_min_utc']} 至 {f['timestamp_max_utc']}（UTC 展示）。异常统计 `{json.dumps(f['source_anomalies'],ensure_ascii=False)}`；源时间戳异常原样保留，本轮没有按日期筛除。

页面本轮只返回标题，不能重新读取文件元数据；大小对照采用方案保留的历史页面元数据，不能写成“本轮重新查到页面大小”。初次完整核验与 OLS 诊断 {f['verification_and_fit_seconds']:.3f} 秒，重新全量统计 pv 并校准概率模型 {f['rank_calibration_seconds']:.3f} 秒；附录 C 原文保留，未把“约一亿条（待核实）”改成未测数字。

## 活跃度、热度与行为比例

用户活跃度采用全量每用户事件数的经验直方图，按固定种子独立重采样到合成用户；分位数 `{f['user_activity_quantiles']}`。没有复制真实用户身份，也没有把订单原来的结构性用户关联伪称活跃度拟合。

| 行为 | 原始事件数 | 全量比例 |
| --- | --- | --- |
{behavior}

访问热度优先以 pv 计数，覆盖 {rank['items']:,} 个商品、{rank['events']:,} 次浏览。原始 OLS log-rank 指数 {ols['zipf_exponent']:.8f}、R² {ols['log_rank_r_squared']:.8f}，但概率 CDF 最大差 {ols['rank_cdf_max_gap']:.8f}，因此没有将其用于正式事件。最终采用有限支持 Zipf 的最大似然均值方程，指数 {rank['zipf_exponent']:.8f}，原始与拟合排名 CDF 最大差 {rank['rank_cdf_max_gap']:.8f}，全支持均值方程残差 {rank['mean_log_rank_residual']:.10f}；前 1% 商品占浏览 {rank['top_1_percent_share']:.6%}，前 10% 占 {rank['top_10_percent_share']:.6%}。所有行为合计的 OLS 诊断指数另报 {rankall['zipf_exponent']:.8f}，不与 pv 指数混用。此拟合是工作负载近似，不宣称通过幂律假设检验；长尾与头部残差如实保留。

## 重建范围、确定性与导入

重建前已在本群说明范围。只重建 {a['files']['users']['rows']:,} 行 users.csv，并在独立 P2 验收库仅更新 commerce.users.activity_events；用户 ID 集与外键不变。新批次引用既有冻结商品、类目、库存、订单、明细、物流、工单、会话、消息 CSV，不复制改写，也不切换主应用。

独立重跑两份完整用户文件，seed={a['seed']}，SHA256 `{a['files']['users']['sha256']}` 相同；包含浏览/加购/购买/收藏的访问事件每份共 {ea['events']:,} 条的两份私有离线文件，SHA256 `{ea['sha256']}` 相同。旧 OLS 诊断产生的两份尝试文件保留为历史，不导入、不参与最终校验。全部正式冻结引用文件的内容哈希、用户文件及 provenance 校验一致。计时/目录路径不参与数据校验和；不是声称运行时间逐字相同。

事件按拟合的浏览热度在 {ea['products']:,} 个合成 SKU 排名上截断抽样，用户按活跃度加权，行为独立按全量比例抽样。合成 SKU 排名不具有淘宝商品语义，没有学习行为转移/购物会话，不把购买比例当因果转化率；此轮只生成离线数据，不做 S3 缓存压测。

| 实体 | 目标行数 | 本轮数据库实查 |
| --- | --- | --- |
{targetrows}

仅本轮用户活动权重 COPY、更新和精确比对耗时 {db['activity_copy_update_verify_seconds']:.3f} 秒；含其他全表前后内容哈希及 ANALYZE 的总耗时 {db['total_seconds_including_full_hashes_and_analyze']:.3f} 秒，不能与旧全量导入混称一次导入。旧表已有导入记录继续见历史报告，本轮不重报为新导入。全量前后哈希比对 {len(db['unaffected_tables_before'])} 张未改动表均一致，用户 ID 全集比对一致，活动权重不匹配 {db['mismatch_rows']} 行。无已验收商品/订单覆盖、无新增永久表、无数据库切换。

本轮每份新增用户 CSV {a['new_csv_bytes']:,} 字节、每份访问事件 {ea['bytes']:,} 字节；包含只引用而未重新复制的所有 CSV 的逻辑体积 {a['total_csv_bytes']:,} 字节。实查数据库（含索引/空闲空间）{db['database_bytes']:,} 字节，数据工作目录 {r['disk']['workdir_bytes']:,} 字节；工作目录不含 Docker 卷，数据库体积与 CSV 不重复算。

## 与旧方案、现状及旧报告的出入

执行新版 SHA256 `{plan['new_sha256']}`（{plan['new_bytes']:,} 字节）。旧版 SHA256 `{plan['old_sha256']}`，保存在 `{plan['old_archive']}`，署名/原文保留。新版只改两处：同配置两组各五轮 A/A 测量噪声，以及改善显著性取噪声下限与 10% 较大者；本轮数据验收不开展优化实验，不用不同日期 B0/B1 的数值宣称优化。

此前“UserBehavior 未找到”的报告是当时服务器搜索事实，不能删除历史；本轮已通过准确附件标识下载并解除该阻塞。源时间戳异常与名义采集区间有差异，完整数据保留，不擅自清洗。既有用户与订单的轮转外键仍是结构性占位，本轮用户活动分布已拟合，两者不混称。

ECD 工单类别继续使用已获批准的“规则代理，非人工真实意图分布，首片段说话人未确认”，仅用于合成类别比例，不进入任何路由评测指标；等待人工比例独立可配置，本轮未做 S1 多档实验。P8 人工标注后的替换仍按原计划，不提前做。

## 测试、失败与未完成项

pytest {gates['tests']} 项通过，跳过 {gates['skipped']}；ruff、strict mypy 通过；固定回归 {eval_['passed']}/{eval_['cases']}，仅回归非路由质量证据。初次测试有 1 失败、92 通过：默认 sibling memory 源码与固定 SDK 不匹配，指定已存在、提交匹配的迁移 checkout 后完整重跑通过，未更换 SDK/放宽测试。OLS 虽 R² 高但概率 CDF 差大，改用有限支持概率模型并重新生成正式批次；不删除失败结果。初次解析误拒绝负时间戳，修正为记录异常并全量保留后重跑，失败原始日志仅本地保留。

P2 本轮三项数据证据：规模通过、同种子校验通过、导入与磁盘实测齐备；是否验收由负责人决定。本次没有做 P3、支付/认证、工单闭环、P8 标注、S1–S7 优化或 A/A 性能实验，没有真实模型调用。无需新的技术选型决定；若负责人要过滤异常日期、模拟真实行为转移或修改订单用户关联，应另行确认范围，不将其夹带到本次。

原始证据：本目录 source-verification-and-fit.json、repeat-proof.json、import.json、pytest.log/xml、ruff.log、mypy.log。重建入口 scripts/verify-fit-userbehavior.py、calibrate-userbehavior-rank.py、rebuild-userbehavior-batch.py、generate-behavior-events.py、import-userbehavior-activity.py。原始数据和访问事件只留服务器，不进公开仓库。
'''
(OUT/'delivery.md').write_text(text)
marker='## 2026-10-23 UserBehavior 到位后的完整核验与重建'
p=Path('docs/datasets.md');old=p.read_text();old=old.split('\n'+marker)[0]
p.write_text(old+'\n\n'+marker+'\n\n'+text.split('## 做了什么与证据\n\n')[1].split('## 与旧方案')[0]+'\n完整交接、版本差异与未完成项见 [本轮报告](reports/p2-userbehavior-2026-10-23/delivery.md)。\n')
print(json.dumps({'report':str(OUT/'delivery.md'),'tests':gates['tests'],'p2_evidence_complete':True}))
