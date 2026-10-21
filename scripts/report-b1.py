"""B1 persistence background comparison; no optimization attribution."""
import json, re, shutil, subprocess
from pathlib import Path
from datetime import datetime,UTC
from zoneinfo import ZoneInfo

out=Path('docs/baselines/b1');out.mkdir(parents=True,exist_ok=True)
def read(p):return json.loads(Path(p).read_text())
pg=Path('.local/b1-http-run1');memory=Path('.local/b1-paired-memory')
old=Path('docs/baselines/b0/results')
for root,name in [(pg,'results'),(memory,'paired-memory')]:
    (out/name).mkdir(exist_ok=True)
    for f in ['environment.json','inputs.json','raw.json','summary.json','aggregate.json']:
        shutil.copyfile(root/f,out/name/f)
    raw=read(root/'raw.json');assert len(raw)==3840 and all(r['valid'] for r in raw)
assert (pg/'inputs.json').read_bytes()==(old/'inputs.json').read_bytes()==(memory/'inputs.json').read_bytes()
assert subprocess.check_output(['git','show','baseline-b0:scripts/benchmark-b0.py'])==Path('scripts/benchmark-b0.py').read_bytes()
log=Path('.local/b1-pytest.log').read_text();match=re.search(r'(\d+) passed.*?in ([\d.]+)s',log)
assert match and 'failed' not in log and 'skipped' not in log
for name in ['ruff','mypy']:
    shutil.copyfile(f'.local/b1-{name}.log',out/(name+'.log'))
shutil.copyfile('.local/b1-pytest.log',out/'pytest.log')
shutil.copyfile('.local/b1-gates.xml',out/'pytest.xml')
tables=[]
for historical,current,persist in zip(read(old/'aggregate.json'),read(memory/'aggregate.json'),read(pg/'aggregate.json'),strict=True):
    assert historical['scenario']==current['scenario']==persist['scenario']
    tables.append({'scenario':persist['scenario'],'historical_b0':historical,'current_memory':current,'b1_postgres':persist,
        'paired_p99_ratio':persist['p99_ms']['median']/current['p99_ms']['median'],
        'paired_rps_ratio':persist['rps']['median']/current['rps']['median']})
r={'generated_at':datetime.now(UTC).isoformat(),'date_beijing':str(datetime.now(ZoneInfo('Asia/Shanghai')).date()),
   'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
   'source_sha256':read(pg/'environment.json')['source_sha256'],
   'harness_unchanged_from_baseline_b0':True,'parameters':{k:read(pg/'environment.json')['config'][k] for k in ['requests','concurrency','warmup','repeats']},
   'contract_checks_postgres':len(read(pg/'raw.json')),'contract_checks_current_memory':len(read(memory/'raw.json')),
   'tests':{'passed':int(match[1]),'seconds':float(match[2]),'skipped':0},'support_eval':read('.local/b1-eval.json'),
   'tables':tables,'original_plan':read('docs/plans/source-2026-10-21.json'),
   'memory_capacity':256,'postgres_capacity':None,'measured_creates_per_repeat_including_unique_warmup':129,
   'scope':'B1 is S1/S2 background, not optimization evidence; offline fixtures and short closed-loop windows',
   'deviations':['historical B0 measured on earlier date and different ambient load; paired current-memory added to expose this confound',
      'unchanged B0 harness pins API to non-client cores; PostgreSQL shares those cores, unlike ideal disjoint CPU allocation; reported explicitly',
      'API CPU and RSS exclude PostgreSQL process; do not call them total persistence resource cost',
      'first pytest used current agent-memory source against pinned SDK and failed; corrected environment to existing clean pinned checkout without code/SDK change'],
   'model_calls':0,'p2_started':False,'p3_or_later_started':False}
(out/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
lines=[]
for row in tables:
    h,c,p=row['historical_b0'],row['current_memory'],row['b1_postgres']
    lines.append(f"| {row['scenario']} | {h['p99_ms']['median']:.2f} | {c['p99_ms']['median']:.2f} | {p['p99_ms']['median']:.2f} | {c['rps']['median']:.2f} | {p['rps']['median']:.2f} |")
(out/'delivery.md').write_text(f'''# P1 补充 B1：PostgreSQL 持久化背景基线

由 Codex 生成 · {r['date_beijing']}（北京时间）· agent-ops-platform@{r['source_commit'][:12]}。测量时源文件哈希与工作区状态详见 results/environment.json，原始样本未修改。新旧方案 SHA256 见 docs/plans/source-2026-10-21.json，旧版完整保留。

## 做了什么

不改 B0 原脚本，包装运行时仅将工单后端设为 PostgreSQL，每轮建立独立数据库、迁移并用非属主账号访问。沿用 B0 的六个 HTTP/SSE 场景和参数：每场景 {r['parameters']['requests']} 请求、并发 {r['parameters']['concurrency']}、预热 {r['parameters']['warmup']}、重复 {r['parameters']['repeats']}。模型、记忆服务与追踪导出均关闭。PG 持久化契约检查 {r['contract_checks_postgres']}/{r['contract_checks_postgres']} 通过。

为避免把跨日期环境变化归因为持久化，另用当前代码的内存开关按同一参数跑五轮，同样 {r['contract_checks_current_memory']}/{r['contract_checks_current_memory']} 通过。B0 历史标签和原始结果保持。

## 证据

各轮分位数的中位数如下；完整 p50/p95/p99、区间、吞吐及逐请求数据见 [report.json](report.json)、[PG 原始数据](results/raw.json)、[当次内存原始数据](paired-memory/raw.json)。单位 p99=毫秒、吞吐=请求/秒。

| 场景 | 历史B0 p99 | 当次内存 p99 | B1 PG p99 | 当次内存吞吐 | B1 PG吞吐 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(lines)}

测试 {r['tests']['passed']} passed，{r['tests']['seconds']} 秒，跳过 {r['tests']['skipped']}；ruff、strict mypy、固定路由评测均通过，日志在本目录。首次测试因迁移源码与固定 SDK 版本不匹配失败；随后改用已存在的干净 1e2730c 兼容 checkout 重跑全量，未更换 SDK 或删除测试。

## 与方案及现状的出入

- B0 内存工单容量 {r['memory_capacity']}，PG 无此容量上限；每轮只有 {r['measured_creates_per_repeat_including_unique_warmup']} 个唯一工单，不触发内存淘汰，不能据此声称高容量能力。
- 为保留原脚本 CPU 放置，PG 与 API 共用非发压核心，没有伪称三者独立绑核。原有常驻容器仍运行；短窗口和环境噪声限制比较，不能把某项偶然变快写成优化。
- server_cpu_seconds/RSS 仅 API 进程，不含数据库；数据库资源增量未单独归因。P0/P1 已实现，不按方案中的旧“内存现状”重写。
- 本结果只作 S1、S2 背景对照，不是优化结论。

## 未做与需要决定的事

未做长稳态容量测量、真实模型测量、S1/S2 优化、P3 及以后。B1 无待负责人决定项；按本轮授权，单独汇报后继续 P2。

复现：`uv run python scripts/benchmark-b1.py --output .local/b1-new-run`；全量门使用 `AGENT_MEMORY_SOURCE=/home/ubuntu/project/WorkPlan/agent-memory/.local/compat-1e2730c uv run pytest -q`、`uv run ruff check src tests`、`uv run mypy --strict src`、`uv run python -m agent_ops.evals.support_runner`。
''')
print(json.dumps({'report':str(out/'delivery.md'),'checks':r['contract_checks_postgres']}))
