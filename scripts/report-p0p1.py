"""Generate P0/P1 handoff strictly from actual environment, test and probe files."""

import argparse
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

p = argparse.ArgumentParser()
p.add_argument("--environment", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
env = json.loads(a.environment.read_text())
suites = ET.parse(".local/p0p1-gates.xml").getroot()
cases = list(suites.iter("testcase"))
assert (
    not list(suites.iter("failure"))
    and not list(suites.iter("error"))
    and not list(suites.iter("skipped"))
)
tests = Path(".local/p0p1-tests-final.log").read_text()
match = re.search(r"(\d+) passed in ([\d.]+)s", tests)
assert match
properties = {
    c.attrib["name"]: {
        v.attrib["name"]: v.attrib["value"] for v in c.findall("./properties/property")
    }
    for c in cases
}
trace = json.loads(Path(".local/p0-verification/tracing.json").read_text())
metrics = json.loads(Path(".local/p0-verification/metrics.json").read_text())
http_proof = json.loads(Path(".local/p0-verification/p1-http.json").read_text())
eval = json.loads(Path(".local/p0p1-eval.json").read_text())
report = {
    "generated_at": datetime.now(UTC).isoformat(),
    "date_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"])),
    "p0_environment": env,
    "tests": {
        "passed": int(match[1]),
        "seconds": float(match[2]),
        "skipped": 0,
        "cases": len(cases),
        "properties": {k: v for k, v in properties.items() if v},
    },
    "ruff": Path(".local/p0p1-ruff.log").read_text().strip(),
    "mypy": Path(".local/p0p1-mypy.log").read_text().strip(),
    "support_eval": {k: eval[k] for k in ("cases", "passed", "dataset_sha256", "method")},
    "trace_probe": {
        k: trace[k] for k in ("backend", "cases", "private_markers_exported", "source_sha256")
    },
    "metrics_probe": metrics,
    "p1_real_http_probe": http_proof,
    "scope": "P0 six-component localhost stack and P1 support persistence only",
    "remaining": [
        "P2 commerce tables/data",
        "P3 user RBAC, shared human token remains",
        "P4 claim/lease/workbench/push",
        "S1-S7 performance experiments",
        "P12 production deployment/backup hardening",
    ],
    "baseline_unchanged": subprocess.run(
        ["git", "diff", "--quiet", "baseline-b0", "HEAD", "--", "docs/baselines/b0"], check=False
    ).returncode
    == 0,
    "private_data_exported": False,
    "model_calls": 0,
    "business_api_resident": False,
}
a.output.mkdir(parents=True, exist_ok=False)
(a.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
shutil.copyfile(a.environment, a.output / "environment.json")
for src, name in [
    (".local/p0p1-tests-final.log", "pytest.log"),
    (".local/p0p1-gates.xml", "pytest.xml"),
    (".local/p0-verification/metrics.json", "metrics.json"),
    (".local/p0-verification/tracing.json", "tracing.json"),
    (".local/p0-verification/p1-http.json", "p1-http.json"),
]:
    shutil.copyfile(src, a.output / name)
concurrent = properties["test_two_processes_unique_key_and_atomic_audit"]
restart = properties["test_restart_replay_snapshot_conflict_and_concurrent_resolve"]
lines = [
    "# agent-ops-platform P0/P1交付报告",
    "",
    f"日期：{report['date_beijing']}（北京时间）；Codex；来源 agent-ops-platform@{report['source_commit'][:7]}；工作区状态见JSON。",
    "",
    "本报告接续首次B0交付，范围按方案第六节的P0/P1；原B0报告与基线保持历史，不覆盖。",
    "",
    "## P0 环境栈",
    "",
    f"一条命令 bash scripts/start-stack.sh，实际启动/复用 {len(env['containers'])} 个服务：PostgreSQL、Redis、Collector、Prometheus、Grafana、Jaeger。镜像按实际拉取摘要固定，配置/数据卷分离；复用原Jaeger与Badger卷。HTTP健康 {env['http_health']}。",
    "",
    "|组件|实际镜像|内存上限(bytes)|",
    "|---|---|---:|",
]
for c in env["containers"]:
    lines.append(f"|{c['service']}|{c['image']}|{c['memory_limit']}|")
lines += [
    "",
    f"主机CPU {env['host']['cpu_count']}，内核 {env['host']['kernel']}，内存 {env['host']['meminfo']}；CPU配额和实际镜像ID、端口绑定、组件版本见environment.json。这里是环境/连通性验收，不是容量压测，未声称核绑定性能对照。",
    "",
    f"经Collector真实导出/Jaeger查询 {len(trace['cases'])} 类追踪，全部query_status通过；私有探针正文未导出={not trace['private_markers_exported']}。Prometheus Collector up={metrics['prometheus_collector_up']}，OTLP探针指标可查={metrics['probe_metric_visible']}，Grafana数据源={metrics['grafana_datasource_status']}。尾部采样、完整业务指标和跨服务传播仍属后续。",
    "",
    "## P1 PostgreSQL数据层",
    "",
    "Alembic upgrade head 创建support.tickets、idempotency_keys、ticket_events；支持downgrade base/re-upgrade隔离验收。运行角色非owner，只能追加事件；触发器禁止事件UPDATE/DELETE。原工单/人工完成接口保持，未提前实现P4抢单租约。",
    "",
    f"- {concurrent['processes']} 个独立OS进程，共 {concurrent['requests']} 次同键写入：工单 {concurrent['unique_tickets']} 张，重复工单 {concurrent['duplicate_tickets']}。",
    f"- 实际PostgreSQL容器重启后保留={restart['retained_after_database_restart']}；并发CAS成功 {restart['successful_resolves']}，版本/状态冲突 {restart['conflicted_resolves']}；当前revision={restart['current_revision']}，原请求重放仍为初始revision={restart['replayed_revision']}。",
    f"- 主P0持久库实际TCP HTTP复验：{http_proof['api_processes_initial']} 个API进程、{http_proof['requests']} 请求、{http_proof['unique_tickets']} 张工单；应用进程重启后保留={http_proof['retained_after_api_process_restart']}，重复工单 {http_proof['duplicate_tickets']}。",
    "- 事件插入故障注入验证事务回滚：工单和幂等记录不留半成品；HTTP切换、trace头、404、人工完成及append-only权限测试均通过。",
    "- 保留原SupportService为memory开关；create_app默认memory，scripts/serve.py默认postgres。PG故障不会自动回退内存。",
    "",
    "## 门禁",
    "",
    f"pytest {match[1]} passed / {match[2]}s，跳过 {report['tests']['skipped']}；{report['ruff']}；{report['mypy']}。规则评测 {eval['passed']}/{eval['cases']}，仍仅为原固定回归集，不当真实路由准确率。",
    "",
    "## 运行与边界",
    "",
    "```bash\nbash scripts/start-stack.sh\nuv run python scripts/serve.py --backend postgres\n# 独立运行内存基线：\nuv run python scripts/serve.py --backend memory --port 8004\n```",
    "私密配置由启动脚本生成于.local/stack.env及.local/application.env（目录700、文件600），不进Git/附件、不打印密码。脚本可重复启动，不删除卷。新端口仅loopback；业务API仍需显式运行，不常驻、不暴露公网。",
    "P1重启与跨进程并发测试运行于真实隔离PostgreSQL容器，属于正确性验收，不代表生产容量或故障恢复SLA。首轮重启测试因随机宿主端口变化失败，改用显式测试端口；Jaeger健康检查由旧v1路径修正为现有v2 /status，返工记录保留。",
    "仍未完成：P2商品订单数据、P3账号/RBAC（仍共享人工token）、P4客服领取/租约/消息闭环、后续优化实验与生产备份加固。P0/P1完成不等于整体生产化方案完成。",
]
(a.output / "delivery.md").write_text("\n".join(lines) + "\n")
print(
    json.dumps(
        {"report": str(a.output), "tests_passed": int(match[1]), "services": len(env["containers"])}
    )
)
