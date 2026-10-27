"""Generate aggregate P4 delivery from actual gates and private maintenance evidence."""
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path('docs/reports/p4-2026-10-27')
root.mkdir(parents=True, exist_ok=True)
private = Path('.local/p4-2026-10-27')
base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert base == '89009744cb5772203a64a0acb3bb05c4a321b7c3'
suite = ET.parse('.local/p4-delivery.xml').getroot().find('testsuite')
assert suite is not None
gates = {k: suite.attrib[k] for k in ['tests', 'failures', 'errors', 'skipped', 'time']}
assert gates['tests'] == '163' and all(gates[k] == '0' for k in ['failures', 'errors', 'skipped'])
assert 'All checks passed!' in Path('.local/p4-delivery-ruff.log').read_text()
assert 'Success:' in Path('.local/p4-delivery-mypy.log').read_text()
eval_result = json.loads(Path('.local/p4-delivery-eval.json').read_text())
maintenance = json.loads((private/'databases.json').read_text())
compat = json.loads((private/'compat-tcp/result.json').read_text())
persistent = json.loads((private/'persistent-http/result.json').read_text())
assert eval_result['passed'] == eval_result['cases'] == 47
assert maintenance['complete'] and compat['passed'] == compat['total'] and persistent['passed'] == persistent['total']
for db in maintenance['databases'].values():
    assert db['complete'] and db['connections_restored'] and db['before'] == db['after']
    assert db['version'] == '0005_workbench' and db['backup']['mode'] == '0o600'
tags = {tag: subprocess.check_output(['git', 'rev-parse', tag], text=True).strip()
        for tag in ['baseline-v0', 'baseline-b0', 'baseline-b1']}
old = json.loads(Path('docs/reports/p3-followup-2026-10-26/numbers.json').read_text())
assert tags == old['frozen_tags']
assert not subprocess.check_output(['git', 'diff', '--', 'docs/baselines'], text=True)
paths = sorted([*Path('src').rglob('*.py'), *Path('tests').rglob('*.py'),
                *Path('migrations').rglob('*.py'), *Path('scripts').rglob('*.py'), Path('pyproject.toml'), Path('uv.lock')])
manifest = {'base_commit': base, 'uncommitted_P4_changes': True,
            'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
(root/'source-files.json').write_text(json.dumps(manifest, indent=2)+'\n')
new_tests = [c.attrib['name'] for c in suite.findall('testcase') if c.attrib.get('classname','').endswith('test_workbench')]
assert len(new_tests) == 15
numbers = {'generated_beijing': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
    'P3_pushed_commit': base, 'P4_uncommitted': True, 'pytest': gates, 'workbench_tests': new_tests,
    'ruff': 'passed', 'strict_mypy': {'passed': True, 'files':23},
    'rules_regression': {'cases':47, 'passed':47, 'quality_accuracy_claim':False},
    'compat_TCP':compat, 'real_P2_TCP_and_WS':persistent, 'maintenance':maintenance,
    'old_baseline_tags':tags, 'S1_executed':False, 'P5_started':False,
    'backup_full_restore_verified':False, 'performance_claim':False}
(root/'numbers.json').write_text(json.dumps(numbers, ensure_ascii=False, indent=2)+'\n')
backup_rows = '\n'.join(f"| {name} | `{db['backup']['path']}` | {db['backup']['bytes']:,} | {db['migration_seconds']:.6f} |"
    for name, db in maintenance['databases'].items())
text = f'''# P4 客服闭环交付（2026-10-27）

> 🤖 由 Codex 生成 · 2026-10-27 · 来源 agent-ops-platform@{base[:12]}（含未提交 P4 修改，逐文件 SHA256 见 source-files.json）· 工具 lark-cli profile codex

P3 已按第一步要求全部提交并推送 main：`{base}`，远端核对一致。P4 后端已完成，停止在 P5 前等待验收；本轮 P4 工作区修改尚未提交推送。方案第六节、B2/B3/B4 与 S1 已重读，最新用户约束优先于附录原来的游标/SKIP LOCKED 描述。

## 做了什么

- 四状态 needs_information/resolved/waiting_human/claimed。创建版本 1，每次转换及新消息/关联订单查看/租约与 SLA 变更递增 revision，并事务性追加审计。resolved 不可重开。领取是指定工单、state、revision 的单条条件 UPDATE RETURNING，竞争只能一席成功。
- 租约初始 10 分钟，领取者回复及详情/消息/审计/关联订单查看会续租；顾客消息和补单号不续租。过期持有者操作被拒绝，后台默认每秒条件回收至 waiting_human。多进程重复扫描只产生一次释放/预警/到期事件。
- 物流查询从真实 commerce.orders/shipments 读取并按登录顾客过滤；缺单号进入 needs_information，补充后重新判断。退货始终 waiting_human，无资金调用。顾客也能在 claimed 后补缺失订单号，保留领取者、状态和租约；不能关联别人的订单或替换已有订单。
- 人工队列按优先级、等待时间和 ID 排序，使用 OFFSET；详情、消息、事件及 PG 顾客工单列表均有偏移分页。客服领取、回复、完成、退回；管理员队列/等待/SLA/各席处理量统计。P5 页面未实现。
- staff 直接 /orders 继续 403。只能经 /workbench/tickets/{{id}}/order 查看本工单已关联且归属一致的真实订单，不接任意订单查询条件；每次查看追加 order_viewed，返回新 ticket_revision。
- 新消息精确记录 customer/agent/system 和实际 actor。ECD 的 source 不改写；消息列表区分 seed/live。消息正文不进审计 payload、日志或推送事件。事务包含状态、消息、幂等快照、审计和推送，失败一起回滚。
- WebSocket 首帧认证和 after，不在 URL 放令牌。事务性全局计数器保证可见序号与提交顺序一致，持久日志按角色/顾客过滤并从最后已处理序号补发；两个 OS 进程共享日志，重连到另一进程也可补发。客户端须保存最后处理序号并去重，不将网络 ACK 冒称 exactly-once。
- 两步领取只在 scripts/experiments/s1-naive-claim.py，必须显式危险开关且只接受 agent_ops_s1_* 库；产品无导入/路由。只验证了拒绝非实验库的安全门，未运行 S1 基线。没有增加 S1 队列部分索引/游标/SKIP LOCKED，未改旧标签及旧测量。

接口、默认 SLA（1/24/48 小时、提前 5 分钟预警）、运行方式与多进程扩展取舍见 [P4 文档](../../p4-support.md)，决策见 docs/design-decisions.md。序号门串行、轮询和历史日志增长是已知代价，性能未验证；未来 Redis Pub/Sub 可唤醒，但可靠补发仍依赖 PG 日志。

## 证据

- 完整 pytest：{gates['tests']} passed，0 failed/error/skipped，JUnit {gates['time']} 秒；2 个既有 JUnit record_property 警告。ruff 通过，strict mypy 23 文件通过，固定规则回归 47/47（不称真实路由准确率）。
- {len(new_tests)} 项 P4 真实 PG 测试：20 个竞争领取请求、两独立连接适配器仅一席成功；错误领取者/过期租约/非法终态/幂等冲突；自动续租/后台回收与 SLA；逐次订单查看审计；未知 ECD 角色保留；事件插入失败回滚；事务序号提交顺序；offset 队列；实验库安全门；0005 往返及 claimed 时降级拒绝。
- 其中真实 TCP + 两个独立 uvicorn OS 进程端到端：顾客退货提问 → 建单 → 座席 WS 收到 created → 另一进程领取 → 座席断线 → 回复/解决 → 另一进程重连补发。审计严格为版本 1/2/3/4 的 created/claimed/message_created/resolved，重复回复 key 不增加事件。顾客只收到自己的事件，其他顾客收不到；非法令牌/未来序号拒绝；日志不含密码或 bearer。
- 更新后的双进程兼容 TCP 脚本 {compat['passed']}/{compat['total']}；真实 P2 只读业务联调与 WS {persistent['passed']}/{persistent['total']}。真实库烟雾只新增认证会话，不新增订单/工单/业务消息；验证队列 OFFSET、管理员统计权限、WS 第一帧登录与 cursor。完整写入闭环在一次性 PG 验证，未把夹具写入百万库。
- P0/P2 均无其他连接时备份、升级 0004 → 0005，恢复准入，业务行数前后一致。P2 100 万用户、200 万订单、100 万工单/事件不变；1000 万种子消息前后均 source。新消息表和 push_clock 在迁移后为 0，未伪造历史推送或旧订单关联。

| 库 | 私有备份 | 字节 | 升级秒 |
| --- | --- | --- | --- |
{backup_rows}

备份均 600，pg_restore --list 可读；完整恢复演练未验证。历史工单 SLA 按迁移时刻初始化 normal +24h，仅是演示新起点，不冒称历史真实 SLA。服务器未执行 0005 downgrade，降级验证用一次性 PG；有 claimed 会拒绝降级，防止悄悄重写状态。临时 API 已停止，未常驻或暴露公网。

原始证据在 .local/p4-* 和 .local/p4-2026-10-27/；[汇总 numbers.json](numbers.json)、[源码快照](source-files.json)。所有结论对应 {base[:12]} 加本轮工作区摘要，不能写成旧提交原样具备 P4。

## 与方案的出入、失败和返工

1. 附录原有游标/行锁跳过按本轮明确要求留到 P10；P4 用 OFFSET + CAS。原 P3 顾客归属索引和既有订单/会话等游标接口保留，不包装为 S1 新优化。
2. 保留历史内存/legacy_support 仅作 B0/B1 基准；P4 产品 PG 必须先领取才解决。两项旧 PG HTTP 用例最初仍直接解决 waiting_human，失败后改为先领取再解决，保留归属/审计断言，没有删除测试。
3. 初始静态检查补齐查询结果类型、限定后台 SQL 错误捕获；真实 TCP 测试改为线程启动子进程、实验安全门测试显式 check=False；新增后台测试首次缺 SupportRequest 导入已补。最终四道门全通过。
4. 明确补齐退货始终 waiting_human、claimed 后补缺单号不改变状态/租约；审计增加关联订单、领取者、租约及创建消息 ID。相应检查后重跑，采用最终 {gates['tests']} 项结果，不把较早 162 项结果当最终版本。
5. 用户“继续”用于本轮 P4 收尾，未解释为允许进入 P5。

## 未做与待决定

未做 P5 UI、P6 退款批准/资金退款、支付、S1/P10 性能实验、百万队列吞吐/推送容量测试、完整备份恢复演练、常驻/公网部署。agent-memory 及其身份协议未改，P7 前 not_configured 缺口仍在。以上未测性能/恢复能力均为未验证，不作通过结论。

本轮无额外技术决策待定；请负责人验收 P4。验收前停在 P5 前，P4 修改尚未提交推送。
'''
(root/'delivery.md').write_text(text)
secret_values = []
for name in ['stack.env','application.env','p2-application.env']:
    p = Path('.local')/name
    values = dict(l.split('=',1) for l in p.read_text().splitlines() if '=' in l)
    secret_values.extend(v for k,v in values.items() if any(s in k for s in ['SECRET','PASSWORD']) and len(v)>12)
secret_values.extend(a['password'] for a in json.loads(Path('.local/demo-accounts.json').read_text())['accounts'])
public = '\n'.join(p.read_text() for p in root.glob('*') if p.is_file()) + subprocess.check_output(['git','diff'],text=True)
logs = [*private.rglob('*.log'), *Path('.local').glob('p4*.log')]
assert not any(value in public or any(value in p.read_text() for p in logs) for value in secret_values)
print(json.dumps({'delivery':str(root/'delivery.md'),'tests':gates,'workbench_tests':len(new_tests),'secret_scan':'passed'}))
