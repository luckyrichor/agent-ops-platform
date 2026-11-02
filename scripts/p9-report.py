#!/usr/bin/env python3
"""P9 结果汇总：按场景/连接数合并三次运行，输出波动判定与 Markdown。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_ops.bench import config, schema


def _number(value: object) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT / "docs" / "reports" / "p9-runs")
    parser.add_argument("--experiment", default="", help="只汇总目录名以此前缀开头的运行")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "reports" / "p9-baseline.md")
    parser.add_argument("--json-out", type=Path, default=ROOT / "docs" / "reports" / "p9-baseline.json")
    args = parser.parse_args()

    cfg = config.load()
    spread_max = float(cfg.tiers["spread_max_percent"])
    groups: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for path in sorted(args.root.glob("*/run.json")):
        if args.experiment and not path.parent.name.startswith(args.experiment):
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        scenario = document["scenario"]
        groups[(scenario["kind"], int(scenario["connections"]))].append(document)

    lines = ["# P9 负载基线", "", (
             "口径：每档连续三次完整运行；spread=(max-min)/mean ≤"
             f" {spread_max:g}%；未达目标连接数、保护中止或生成器饱和均不通过。"), ""]
    summary: dict[str, object] = {"groups": {}, "passed": True}
    for (kind, connections), runs in sorted(groups.items()):
        verdict = schema.aggregate(runs, spread_max_percent=spread_max)
        key = f"{kind}-{connections}"
        summary["groups"][key] = {  # type: ignore[index]
            "runs": len(runs),
            "verdict": verdict,
            "first_event_p95_ms": [run["latency_ms"]["first_event"].get("p95") for run in runs],
            "full_p95_ms": [run["latency_ms"]["full"].get("p95") for run in runs],
            "throughput_per_second": [run["throughput"]["per_second"] for run in runs],
            "achieved": [run["achieved_connections"] for run in runs],
            "errors": [run["errors"] for run in runs],
        }
        if not verdict.get("passed"):
            summary["passed"] = False  # type: ignore[index]
        lines.extend([
            f"## {kind} · {connections} 连接",
            "",
            "| 轮次 | 实际连接 | 完成请求 | 吞吐/s | 首帧 p95(ms) | 全链路 p95(ms) | 保护中止 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        for index, run in enumerate(runs, 1):
            lines.append(
                "| {i} | {achieved} | {completed} | {rate:.2f} | {p95} | {full} | {abort} |".format(
                    i=index,
                    achieved=run["achieved_connections"],
                    completed=run["completed_requests"],
                    rate=run["throughput"]["per_second"],
                    p95=_number(run["latency_ms"]["first_event"].get("p95")),
                    full=_number(run["latency_ms"]["full"].get("p95")),
                    abort="是" if run["protection"]["aborted"] else "否",
                )
            )
        lines.extend(["", (
            f"判定：{'通过' if verdict.get('passed') else '未通过'} "
            f"({verdict.get('reason') or verdict.get('unstable_metrics')})"), ""])

    args.out.write_text("\n".join(lines), encoding="utf-8")
    args.json_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "groups": list(summary["groups"])},  # type: ignore[index]
                     ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
