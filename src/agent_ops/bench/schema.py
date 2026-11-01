"""版本化运行证据：只允许新建、原子写、缺失不补 0。

公开 JSON 只含匿名统计；URL、主机名、端口、凭据与原始请求由调用方排除，
写入前再做一次结构检查，命中即拒绝。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_RUN_KEYS = {
    "schema_version",
    "experiment_id",
    "run_index",
    "scenario",
    "source",
    "started_at",
    "status",
    "target_connections",
    "achieved_connections",
    "healthy_connections",
    "latency_ms",
    "errors",
    "resources",
    "protection",
}
SCENARIO_KEYS = {"kind", "connections", "slow_reader_ratio", "seed", "timings", "profile"}
STATUSES = {"completed", "partial", "aborted"}
KINDS = {"business", "transport"}
FORBIDDEN_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"(?i)(password|api[_-]?key|token|secret)\s*[:=]"),
)


def validate_run(document: dict[str, Any], *, public: bool = True) -> None:
    missing = REQUIRED_RUN_KEYS - document.keys()
    if missing:
        raise ValueError(f"missing run keys: {sorted(missing)}")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    if document["status"] not in STATUSES:
        raise ValueError("unknown status")
    scenario = document["scenario"]
    if not isinstance(scenario, dict) or SCENARIO_KEYS - scenario.keys():
        raise ValueError("incomplete scenario")
    if scenario["kind"] not in KINDS:
        raise ValueError("unknown scenario kind")
    if not isinstance(scenario["connections"], int) or scenario["connections"] <= 0:
        raise ValueError("connections must be a positive integer")
    if not 0 <= float(scenario["slow_reader_ratio"]) <= 1:
        raise ValueError("slow reader ratio out of range")
    if document["status"] == "completed" and document["achieved_connections"] < (
        document["target_connections"]
    ):
        raise ValueError("completed run must reach target connections")
    if public:
        text = json.dumps(document, ensure_ascii=False)
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                raise ValueError("public document contains host or credential detail")


def write_once(path: Path, document: dict[str, Any], *, public: bool = True) -> None:
    """同目录临时文件 + fsync + rename；目标已存在则拒绝覆盖。"""
    validate_run(document, public=public)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing evidence: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def aggregate(runs: list[dict[str, Any]], *, spread_max_percent: float) -> dict[str, Any]:
    """三次运行合并判定；不是三次完整运行一律不通过。"""
    if len(runs) != 3:
        return {"passed": False, "reason": "THREE_RUNS_REQUIRED", "runs": len(runs)}
    if any(run["status"] != "completed" for run in runs):
        return {"passed": False, "reason": "RUN_NOT_COMPLETED", "runs": len(runs)}

    def collect(path: tuple[str, ...]) -> list[float]:
        values: list[float] = []
        for run in runs:
            node: Any = run
            for key in path:
                node = node[key]
            values.append(float(node))
        return values

    from agent_ops.bench.stats import spread_percent

    metrics = {
        "achieved_connections": collect(("achieved_connections",)),
        "throughput_per_second": collect(("throughput", "per_second")),
        "first_event_p95_ms": collect(("latency_ms", "first_event", "p95")),
        "first_event_p99_ms": collect(("latency_ms", "first_event", "p99")),
        "full_p95_ms": collect(("latency_ms", "full", "p95")),
        "peak_memory_mib": collect(("resources", "peak_memory_mib")),
    }
    spreads: dict[str, float] = {}
    unstable: list[str] = []
    for name, values in metrics.items():
        try:
            spread = spread_percent(values)
        except ValueError:
            unstable.append(name)
            continue
        spreads[name] = round(spread, 4)
        if spread > spread_max_percent:
            unstable.append(name)
    protected = all(not run["protection"]["aborted"] for run in runs)
    return {
        "passed": not unstable and protected,
        "unstable_metrics": unstable,
        "spreads": spreads,
        "protection_ok": protected,
        "spread_max_percent": spread_max_percent,
    }
