"""Publish aggregate P3 evidence only; never copy private rosters, SQL URLs or raw data."""
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

private = Path('.local/p3-followup-2026-10-26')
output = Path('docs/reports/p3-followup-2026-10-26')
output.mkdir(parents=True, exist_ok=True)
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert os.environ.get('LARK_CHANNEL_PROFILE') == 'codex'
source_paths = sorted([*Path('src').rglob('*.py'), *Path('tests').rglob('*.py'),
    *Path('migrations').rglob('*.py'), *Path('scripts').glob('*.py'), Path('uv.lock'), Path('pyproject.toml')])
sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
(output / 'source-files.json').write_text(json.dumps({'base_commit': head, 'uncommitted_changes': True,
    'note': 'final working source snapshot; tests ran on this src/tests implementation; maintenance reports retain execution-time hashes/versions',
    'sha256': sources}, indent=2)+'\n')
databases = json.loads((private / 'databases.json').read_text())
owners = json.loads((private / 'ownership.json').read_text())
http = json.loads((private / 'http/result.json').read_text())
persistent = json.loads((private / 'persistent-http/result.json').read_text())
evaluation = json.loads((private / 'eval2.json').read_text())
assert databases['complete'] and owners['ownership_mismatches'] == 0
assert http['passed'] == http['total'] and persistent['passed'] == persistent['total']
assert evaluation['passed'] == evaluation['cases']
benchmarks = {}
for mode in ['memory-auth', 'memory-noauth', 'pg-auth', 'pg-noauth']:
    folder = private / ('bench-' + mode)
    raw = json.loads((folder / 'raw.json').read_text())
    metadata = json.loads((folder / 'environment.json').read_text())
    assert len(raw) == 3840 and all(r['valid'] for r in raw)
    assert metadata['authentication']['login_in_timing'] is False
    assert not list(folder.glob('credentials-*'))
    benchmarks[mode] = {'requests': len(raw), 'valid': sum(r['valid'] for r in raw),
        'rounds': metadata['config']['repeats'], 'authentication': metadata['authentication'],
        'inputs_sha256': metadata['inputs_sha256'], 'source_head': metadata['source_head'],
        'harness_sha256': metadata['source_sha256']['scripts/benchmark-b0.py'],
        'role': 'contract smoke only; shared-host concurrent workload; no performance comparison'}
assert len({b['inputs_sha256'] for b in benchmarks.values()}) == 1
suite = ET.parse(private / 'gates.xml').getroot().find('testsuite')
assert suite is not None
pytest = {k: suite.attrib[k] for k in ['tests', 'failures', 'errors', 'skipped', 'time']}
assert pytest['tests'] == '135' and all(pytest[k] == '0' for k in ['failures', 'errors', 'skipped'])
for name in ['ruff-final.log', 'mypy-final.log']:
    assert 'Success:' in (private / name).read_text() or 'All checks passed!' in (private / name).read_text()
tags = {tag: subprocess.check_output(['git', 'rev-parse', tag], text=True).strip()
        for tag in ['baseline-v0', 'baseline-b0', 'baseline-b1']}
assert not subprocess.check_output(['git', 'diff', '--', 'docs/baselines'], text=True).strip()
secret_values = []
for path in ['.local/stack.env', '.local/application.env', '.local/p2-application.env']:
    p = Path(path)
    assert p.stat().st_mode & 0o777 == 0o600
    values = dict(l.split('=', 1) for l in p.read_text().splitlines() if '=' in l)
    secret_values.extend(v for k, v in values.items() if any(x in k for x in ['PASSWORD', 'SECRET']) and len(v) >= 12)
roster = Path('.local/demo-accounts.json')
assert roster.stat().st_mode & 0o777 == 0o600
secret_values.extend(e['password'] for e in json.loads(roster.read_text())['accounts'])
logs = [*private.rglob('*.log'), *Path('docs/environments/p0-20261026T131732Z').rglob('*.json')]
assert not any(value in p.read_text() for value in secret_values for p in logs)
report = {'generated_beijing': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
    'base_commit': head, 'uncommitted_changes': True, 'pytest': pytest,
    'ruff': 'passed', 'strict_mypy': 'passed (21 files)',
    'rules_regression': {'passed': evaluation['passed'], 'cases': evaluation['cases'], 'quality_claim': False},
    'disposable_tcp': {'passed': http['passed'], 'total': http['total']},
    'persistent_P2_tcp': persistent, 'benchmarks': benchmarks, 'frozen_tags': tags,
    'credentials_in_logs': False, 'private_file_modes_600': True,
    'P4_started': False, 'agent_memory_interface_changed': False,
    'ownership': owners, 'databases': databases}
(output / 'numbers.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
p2 = databases['databases']['agent_ops_p2_seed42']; p0 = databases['databases']['agent_ops']; backup = p0['backup']
ddl = '\n'.join(f"| {r['phase']} | {r['sql'].splitlines()[-1].strip()} | {r['seconds']:.6f} |"
                for r in p2['ddl'])
text = f'''# P3 收尾交付（2026-10-26）

> 🤖 由 Codex 生成 · 2026-10-26 · 来源 agent-ops-platform@{head[:12]}（含未提交修改，逐文件 SHA256 见 source-files.json）· 工具 lark-cli profile codex

本轮按负责人明确要求收尾 P3，停止在 P4 前。附件与仓库方案逐字节一致；新增要求见 [执行口径](../../p3-followup-plan-2026-10-26.md)。不自行提交或推送代码，等待验收。

## 做了什么

- P2 无其他客户端时关闭新连接准入，0004 升级 → 降级到 0003 → 再升级；每步关键表行数相同，降级后确认 auth schema、归属列及两个业务索引消失。随后恢复连接准入并创建 6 个演示账号（3 customer / 2 agent / 1 admin），仅关联 3 个有订单的现有用户，未给 100 万用户批量设密码。
- P0 先 pg_dump 自定义格式备份，再顺序从实际 0001 升级至 0004（含 0002/0003 前置迁移）。主库仍为 0 用户/订单、1 张历史测试工单和 2 条事件；旧工单归属仍 NULL，没有导入 P2 数据。
- `scripts/stack.py` 已运行，六组件健康。签名密钥只在 .local；stack.env、application.env、p2-application.env、demo-accounts.json 均为 600；日志秘密真值扫描通过。
- 导购普通与 SSE 入口都必须 customer 登录，匿名 401、客服/管理员 403。SSE 在发出 200 前校验身份。平台令牌不转发到其他服务。
- 核对固定 SDK `1e2730c` 与相邻 memory `2fae08b` 的身份解析：仅 RS256 JWT 的 UUID tenant_id/sub，无服务凭据加平台 bigint 用户 ID 接口。因此 HTTP 不构造 memory 客户端，总体/读/写状态均 not_configured；memory_id 也不会触发读取。底层 SDK 能力与独立集成测试保留。
- P2 种子工单按会话确定唯一用户才补归属：{owners['updated']:,} 张；错配、可补未补、无来源标记却补归属均为 0。当前完整 P2 批次都能确定归属；无会话/冲突/无标记/其他顾客的拒绝规则另有真实 PG 夹具测试。P0 旧工单不补。客服直接访问订单继续 403。
- 当前 benchmark-b0/b1 默认先建夹具账号并登录，在 warmup、延迟/CPU 计时之前完成。`--auth off` 仅显式基准工厂关闭认证，正常 API 不读取该环境开关。当前结果独立标注，历史标签与 docs/baselines 原结果未动。

## 证据

- pytest {pytest['tests']} 通过，失败/错误/跳过均 0；JUnit {pytest['time']} 秒。2 个既有 record_property/xunit2 警告。ruff 和 strict mypy 通过（21 个源文件），固定规则回归 {evaluation['passed']}/{evaluation['cases']}，不称真实准确率。
- 一次性 PG + 双 worker 真实 TCP {http['passed']}/{http['total']}；真实 P2 + 同一签名密钥的双进程 TCP {persistent['passed']}/{persistent['total']}。覆盖演示账号登录、跨进程验签、顾客订单与种子工单隔离、跨顾客 404、staff 订单 403、导购两入口权限与记忆 not_configured、日志无密码/JWT。P2 烟雾只写认证会话，不新增业务订单/工单，临时 API 进程已停止。
- 内存/PG × 认证开启/关闭四组，每组五轮、3840/3840 合约通过，共 15360。输入哈希相同、元数据明确 login_in_timing=false。期间服务器存在其他测试及回填核验负载，仅证明脚本合约可运行，不做性能收益结论；不与异日期 B0/B1 比较。
- 原始私有日志：`.local/p3-followup-2026-10-26/`；公开汇总：[numbers.json](numbers.json)、[源码摘要](source-files.json)。测试基于上述 HEAD 及本轮工作区实现，源码未提交；原始测量保留当时 dirty/source_sha256，不冒充旧标签数据。

### P2 迁移耗时

升级 {p2['upgrade']['seconds']:.6f} 秒；降级 {p2['downgrade']['seconds']:.6f} 秒；再次升级 {p2['upgrade_again']['seconds']:.6f} 秒。单条 DDL 是客户端包围数据库执行的 wall time，包含往返；总迁移还含事务及 Alembic 开销，不把两者混算。

| 阶段 | DDL | 秒 |
| --- | --- | --- |
{ddl}

### P0 备份

- 路径：`{backup['path']}`
- 大小：{backup['bytes']:,} 字节，权限 {backup['mode']}；备份及目录读取核验共 {backup['seconds']:.6f} 秒。
- SHA256：`{backup['sha256']}`。`pg_restore --list` 可读取且含 support；未进行整库恢复演练，不把目录检查称恢复成功。
- 从 0001 到 0004 总迁移 {p0['upgrade']['seconds']:.6f} 秒；0004 各索引/加列耗时见 numbers.json。迁移后连接准入恢复。

## 出入、返工与未做

- 方案附件无字节变化；按负责人补充口径执行。P0 实际还在 0001，故执行到 0004 所需的 0002/0003，未重建/覆盖数据。
- 为逐条计时拆开 0004 SQL 时，首次误将注释中的分号作为边界，语法错误后事务回滚；修正注释后迁移往返及 PG 测试通过。首次维护脚本假定主库已有 commerce 表，检查发现实际仍 0001 后退出且恢复准入；修正为识别实际起点后先备份再升级。
- 回填 UPDATE 已提交，后续关联验证查询计划低效，仅取消该只读查询；改用聚合 JOIN 验证，未重复 UPDATE。原回填耗时未落盘，报告为 null，不估算；新验证耗时 {owners['set_based_validation_seconds']:.6f} 秒。脚本现提交后立即保存更新数量/耗时，再验证。
- HTTP 老测试补真实登录时，初版辅助代码误作用于 MockTransport、导出计数混入登录 span、直接 SDK 错误需要显式映射等已修正；最终所有测试通过。持久库烟雾初版误以为订单列表含 user_id，修为逐订单详情核验；首次评测模块路径错误后改为实际 evals.support_runner。未修改业务接口迁就脚本。
- 未改 agent-memory 接口或固定依赖；P7 前必须确定服务身份和逐用户隔离协议，若改接口先汇报并同步两边文档。
- 未做 P4 队列/抢单/推送/退货及客服订单查看。P4 约束已记：只能在工单详情查看关联订单、每次审计，不能查任意顾客订单。未做前端、支付、登录限流、优化实验、真实模型调用、常驻或公网 API。

## 需要负责人决定

本轮无需新增技术取舍。请验收 P3；未获验收不进入 P4。memory 身份协议作为 P7 前缺口保留，当前采用已授权的 not_configured 方式。
'''
(output / 'delivery.md').write_text(text)
# Public output and the complete working diff must not contain real signing secrets/passwords.
payload = '\n'.join(p.read_text() for p in output.glob('*') if p.is_file())
payload += subprocess.check_output(['git', 'diff'], text=True)
assert not any(secret in payload for secret in secret_values)
print(json.dumps({'report': str(output / 'delivery.md'), 'pytest': pytest, 'benchmarks': 4,
                  'secret_scan': 'passed'}))
