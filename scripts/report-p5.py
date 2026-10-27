"""Aggregate actual P5 gates and private browser evidence; never publish credentials/images."""
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

out=Path('docs/reports/p5-2026-10-27');out.mkdir(parents=True,exist_ok=True)
private=Path('.local/p5-2026-10-27/final')
head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
assert head=='1b344abfc78a62f48ed226df297ccc5f92ff4856'
suite=ET.parse('.local/p5-final-backend.xml').getroot().find('testsuite');assert suite is not None
gates={k:suite.attrib[k] for k in ['tests','failures','errors','skipped','time']}
assert gates['tests']=='165' and all(gates[k]=='0' for k in ['failures','errors','skipped'])
pw=json.loads((private/'playwright.json').read_text())
assert pw['stats']['expected']==5 and all(pw['stats'][k]==0 for k in ['unexpected','skipped','flaky'])
result=json.loads((private/'result.json').read_text());assert result['playwright_exit_code']==0 and not result['P0_P2_touched']
assert len(result['screenshots'])==15
assert 'All checks passed!' in Path('.local/p5-final-ruff.log').read_text()
assert 'Success:' in Path('.local/p5-final-mypy.log').read_text()
assert 'error TS' not in Path('.local/p5-final-types.log').read_text()
assert 'built in' in Path('.local/p5-final-build.log').read_text()
evaluation=json.loads(Path('.local/p5-final-eval.json').read_text());assert evaluation['passed']==evaluation['cases']==47
sources=[*Path('src').rglob('*.py'),*Path('tests').rglob('*.py'),*Path('migrations').rglob('*.py'),*Path('scripts').rglob('*.py'),Path('uv.lock'),Path('pyproject.toml')]
sources += [p for p in Path('frontend').rglob('*') if p.is_file() and not {'node_modules','dist','test-results','playwright-report'}&set(p.parts)]
manifest={'base_commit':head,'uncommitted_P5_changes':True,'sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)}}
(out/'source-files.json').write_text(json.dumps(manifest,indent=2)+'\n')
shots=[]
labels=['客户登录（遮罩）','顾客排队','工作台队列','工作台详情及订单面板','顾客收到实时回复','已解决与审计','双标签隔离','商品浏览','商品详情','真实下单及我的订单','支付尚未开通','SSE 演示及能力边界','管理员统计','移动端登录','移动端商品浏览']
for name,label in zip(result['screenshots'],labels,strict=True):
 p=Path(name);shots.append({'page':label,'path':str(p.resolve()),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
package=json.loads(Path('frontend/package.json').read_text())
lock=json.loads(Path('frontend/package-lock.json').read_text())
versions={name:lock['packages']['node_modules/'+name]['version'] for name in ['react','react-dom','typescript','vite','@playwright/test']}
numbers={'generated_beijing':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'P4_pushed_commit':head,'P5_uncommitted':True,
'backend':gates,'ruff':'passed','strict_mypy':{'passed':True,'files':23},'fixed_rules':{'cases':47,'passed':47,'quality_accuracy_claim':False},
'frontend_typecheck':'passed','production_build':'passed','playwright':pw['stats'],'browser':result['browser'],'versions':versions,
'screenshots':shots,'screenshots_are_fixture_database':True,'P0_P2_business_writes':False,'public_exposure':False,
'user_computer_SSH_connection_verified':False,'P6_started':False,'semantic_product_search_enabled':False,'payment_enabled':False}
(out/'numbers.json').write_text(json.dumps(numbers,ensure_ascii=False,indent=2)+'\n')
(out/'screenshots.json').write_text(json.dumps(shots,ensure_ascii=False,indent=2)+'\n')
rows='\n'.join(f"| {s['page']} | `{s['path']}` |" for s in shots)
text=f'''# P5 两个前端交付（2026-10-27）

> 🤖 由 Codex 生成 · 2026-10-27 · 来源 agent-ops-platform@{head[:12]}（含未提交 P5 修改，逐文件 SHA256 见 source-files.json）· 工具 lark-cli profile codex

P4 已按第一步全部提交推送 main：`{head}`。本轮只做 P5，已重读 B3/B4/B5；当前 P5 工作区修改尚未提交推送，停在 P6 前等待验收。

## 做了什么

- React/TypeScript 一个包、两个角色入口：/customer 客户网页和 /workbench 工作台。客户登录、导购/客服同窗口、排队/人工处理中/已解决、实时回复、商品浏览/详情/真实下单、我的订单/物流/退货、支付骨架；工作台登录、实时待领取/我领取的、分页、详情/会话/路由理由/订单面板/操作记录、领取/回复/解决/退回，管理员额外看统计。
- 实际商品浏览调用 PG，不提供前端假商品兜底；下单真实预留库存，读取真实订单。支付显示“尚未开通”，无支付/退款请求或假成功。真实导购检索和记忆显示“尚未开通”；SSE 只演示现有固定工具 started/result 状态，不把演示商品当真实推荐、不给它下单入口。
- 会话仅每标签内存，刷新重新登录；无 localStorage/sessionStorage/跨标签广播保存或同步令牌。串行刷新令牌、HTTP 重试复用幂等键；退出清空当前页面/WS/数据，不影响另一标签账号。密码和令牌不写源码、URL、日志、截图；输入统一遮罩，trace/video 关闭。
- 工作台刷新和写串行，采用最新 revision；忽略 lease_renewed/order_viewed 推送回声避免续租循环，其他事件处理成功才推进 after，重连继续补发。冲突显示可重试提示并保留草稿，不静默覆盖。
- 前端/接口仅 127.0.0.1；同源 /api 代理 HTTP/WS，不开公网。已写服务器两个前台进程、客户端 SSH -L、备用本地端口和截图 scp 步骤，详见 [运行与接口约定](../../p5-frontend.md)。

## 验证证据

- 后端 {gates['tests']} passed / 0 skipped / {gates['time']} 秒；2 个既有 JUnit 警告。ruff、strict mypy 23 文件通过；固定规则 47/47，仅回归不称真实质量准确率。
- 前端 tsc --noEmit 与生产构建通过。React {versions['react']} / TypeScript {versions['typescript']} / Vite {versions['vite']} / Playwright {versions['@playwright/test']}，锁定包版本见 package-lock.json。
- Playwright 5/5，通过、无跳过/意外失败/重试漂移；实际 {result['browser']}，执行 {pw['stats']['duration']/1000:.3f} 秒，不当性能基准。
- 同一浏览器上下文两个标签分别登录不同顾客，各自订单/工单隔离、跨顾客订单 404、退出一个不退出另一个，存储没有持久登录状态。主流程覆盖顾客提问→建工单→座席 WS 收到推送→领取→断线→回复实时补发→解决→完整操作记录。额外覆盖真实 PG 商品下单/库存变化/订单详情、支付骨架、SSE 明确边界、退回重领/我的领取/管理员统计、移动端无横向溢出。
- 浏览器使用一次性真实 PostgreSQL、真实 API、生产前端构建，不以浏览器 Mock 替代业务。截图商品为测试库夹具，不冒称 P2 全量数据截图或 P7 检索效果。没有改 P0/P2 业务数据，测试容器和临时服务器已停止。

原件 `.local/p5-2026-10-27/final/`；[汇总](numbers.json)、[截图清单](screenshots.json)、[源码快照](source-files.json)。

## 截图路径

15 张均仅留服务器 .local，登录字段遮挡，抽查客户会话/工作台/商品/移动页面未见凭据。无需打开网页，可用文档中的 scp 命令取到电脑查看。

| 页面 | 服务器绝对路径 |
| --- | --- |
{rows}

## 与方案的出入、失败与返工

1. 用一个 React 构建提供两个角色入口，没有拆成两个独立部署项目。补充了 P5 必需的薄后端契约：公开商品 list/detail（调用已有 PG 能力）、队列 claimed/mine 过滤（恢复已领取工单）、push payload.change（区分只读续租回声）。没有改领取算法/索引或提前做 S1；接口约定已写 docs/design-decisions.md。
2. P6/P7 依赖只做明确的未开通骨架。现有导购 SSE 不是模型逐 token 输出，也不做真实商品检索，页面和文档如实标记；注册 API 未实现，登录页明确注册尚未开通。
3. 初次 TypeScript 检查缺 CSS 类型引用，补 vite/client 声明后通过。Playwright 配套 Chromium 156 下载未推进，停止该下载，复用现有 Chromium 153 实测通过，不冒称已测新版浏览器。
4. 首轮 4/5，商品场景因支付按钮的可访问名称含“尚未开通”，测试定位未匹配而超时；修正定位后 5/5。再把系统码显示转成中文、队列空态改为准确文案，最终重跑类型/构建/全部 Playwright 5/5。失败原件保留 trial1，trial2 是中间通过结果，最终以 final 为准。

## 未做、未验证与待决定

未进入 P6，未接支付/退款/真实导购检索/记忆。未做公网/常驻部署、S1 优化或容量实验。Firefox/Safari/真实手机、用户电脑 SSH 实连、百万库浏览器写入联调未验证；本机 Chromium 和移动视口已验证。刷新页面需重新登录是当前内存会话的明确取舍。

无新技术取舍待决定，请负责人验收 P5；验收前不进入 P6，P5 修改尚未提交推送。
'''
(out/'delivery.md').write_text(text)
secret_values=[]
for file in ['.local/stack.env','.local/application.env','.local/p2-application.env']:
 values=dict(l.split('=',1) for l in Path(file).read_text().splitlines() if '=' in l)
 secret_values += [v for k,v in values.items() if any(x in k for x in ['SECRET','PASSWORD']) and len(v)>12]
for roster in [Path('.local/demo-accounts.json'),private/'accounts.json']:
 secret_values += [a['password'] for a in json.loads(roster.read_text())['accounts']]
 assert roster.stat().st_mode&0o777==0o600
public='\n'.join(p.read_text() for p in out.glob('*') if p.is_file())
public+='\n'.join(p.read_text() for p in Path('frontend/dist').rglob('*') if p.is_file())
public+=subprocess.check_output(['git','diff'],text=True)
assert not any(value in public for value in secret_values)
for log in [*private.rglob('*.log'),*Path('.local').glob('p5*.log')]:
 assert not any(value in log.read_text() for value in secret_values)
assert not subprocess.check_output(['git','diff','--','docs/baselines'],text=True)
print(json.dumps({'report':str(out/'delivery.md'),'backend_tests':gates,'playwright_passed':5,'screenshots':15,'secret_scan':'passed'}))
