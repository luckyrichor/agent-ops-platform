#!/usr/bin/env python3
"""P9 前置换页闸：600 秒 vmstat 观测 + PSI/内存最低值 + 保护服务 FD 限额。

只读采样：不启动桩/负载、不改服务与系统配置、不调用模型。
公开摘要仅匿名统计；原始采样、进程号与实际限额写 .local/。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src" / "agent_ops" / "bench" / "acceptance-v2.json"
PRIVATE_ROOT = ROOT / ".local" / "p9-preflight"
PUBLIC_ROOT = ROOT / "docs" / "reports" / "p9-preflight"

PROTECTED_UNITS = [
    "getaudio-api.service",
    "getvideo-api.service",
    "getvideo-ai-worker.service",
]


def read_pressure() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for kind in ("memory", "io", "cpu"):
        text = Path(f"/proc/pressure/{kind}").read_text(encoding="utf-8")
        parsed: dict[str, float] = {}
        for line in text.splitlines():
            fields = dict(item.split("=") for item in line.split()[1:])
            for key, value in fields.items():
                parsed[f"{line.split()[0]}_{key}"] = float(value)
        out[kind] = parsed
    return out


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024.0
    raise RuntimeError("MemAvailable missing")


def command(*args: str) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.stdout


def unit_pid(unit: str) -> int | None:
    """Find the main PID through system or user manager without guessing."""
    for scope in (["systemctl"], ["systemctl", "--user"]):
        out = command(*scope, "show", unit, "-p", "MainPID", "-p", "ActiveState")
        fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        pid = fields.get("MainPID", "0")
        if fields.get("ActiveState") == "active" and pid.isdigit() and int(pid) > 0:
            return int(pid)
    return None


def fd_limits(units: list[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for unit in units:
        pid = unit_pid(unit)
        if pid is None:
            result[unit] = {"active": False}
            continue
        limits_path = Path(f"/proc/{pid}/limits")
        if not limits_path.exists():
            result[unit] = {"active": True, "pid": pid, "limits_readable": False}
            continue
        limits = {}
        for line in limits_path.read_text(encoding="utf-8").splitlines()[1:]:
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) >= 4:
                limits[parts[0]] = {"soft": parts[2], "hard": parts[3]}
        result[unit] = {"active": True, "pid": pid, "limits_readable": True, "limits": limits}
    return result


def parse_vmstat(lines: list[str]) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 17 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        rows.append(
            {
                "swpd": float(fields[2]),
                "free": float(fields[3]),
                "si": float(fields[6]),
                "so": float(fields[7]),
                "bi": float(fields[8]),
                "bo": float(fields[9]),
            }
        )
    return rows


def summarize(rows: list[dict[str, float]], interval_seconds: int, mem_min_mib: float, psi: dict) -> dict:
    swap_in = [r["si"] for r in rows[:interval_seconds]]
    swap_out = [r["so"] for r in rows[:interval_seconds]]
    nonzero_in = sum(1 for value in swap_in if value > 0)
    nonzero_out = sum(1 for value in swap_out if value > 0)
    minutes = []
    for start in range(0, interval_seconds, 60):
        window = rows[start : start + 60]
        minutes.append(
            {
                "minute": start // 60 + 1,
                "swap_in_nonzero_seconds": sum(1 for r in window if r["si"] > 0),
                "swap_out_nonzero_seconds": sum(1 for r in window if r["so"] > 0),
                "swap_in_kib": int(sum(r["si"] for r in window)),
                "swap_out_kib": int(sum(r["so"] for r in window)),
            }
        )
    return {
        "duration_seconds": interval_seconds,
        "samples": len(swap_in),
        "swap_in_nonzero_seconds": nonzero_in,
        "swap_out_nonzero_seconds": nonzero_out,
        "swap_in_kib": int(sum(swap_in)),
        "swap_out_kib": int(sum(swap_out)),
        "max_swap_in_nonzero_seconds_per_minute": max(
            (m["swap_in_nonzero_seconds"] for m in minutes), default=0
        ),
        "mem_available_min_mib": round(mem_min_mib, 1),
        "memory_psi_full_avg60_percent_max": max(psi["samples_full_avg60"], default=0.0),
        "minutes": minutes,
    }


def evaluate(summary: dict, gates: dict) -> dict:
    duration = max(1, summary["duration_seconds"])
    out_percent = summary["swap_out_nonzero_seconds"] / duration * 100
    in_percent = summary["swap_in_nonzero_seconds"] / duration * 100
    minute_percent = summary["max_swap_in_nonzero_seconds_per_minute"] / 60 * 100
    checks = {
        "swap_out_nonzero_seconds_percent": out_percent
        <= gates["swap_out_nonzero_seconds_percent_max"],
        "swap_out_kib_total": summary["swap_out_kib"] <= gates["swap_out_kib_total_max"],
        "swap_in_nonzero_seconds_percent": in_percent
        <= gates["swap_in_nonzero_seconds_percent_max"],
        "swap_in_kib_total": summary["swap_in_kib"] <= gates["swap_in_kib_total_max"],
        "swap_in_nonzero_seconds_per_minute_percent": minute_percent
        <= gates["swap_in_nonzero_seconds_per_minute_percent_max"],
        "mem_available_min_mib": summary["mem_available_min_mib"] >= gates["mem_available_min_mib"],
        "memory_psi_full_avg60_percent": summary["memory_psi_full_avg60_percent_max"]
        <= gates["memory_psi_full_avg60_percent_max"],
    }
    summary["percentages"] = {
        "swap_out_nonzero_seconds": round(out_percent, 3),
        "swap_in_nonzero_seconds": round(in_percent, 3),
        "max_minute_swap_in": round(minute_percent, 3),
    }
    return {"checks": checks, "passed": all(checks.values())}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    preflight = config["preflight"]
    duration = int(preflight["duration_seconds"])
    gates = preflight["gates"]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    private_dir = PRIVATE_ROOT / stamp
    private_dir.mkdir(parents=True, exist_ok=False)
    public_dir = PUBLIC_ROOT / f"{stamp}-rerun"
    public_dir.mkdir(parents=True, exist_ok=False)

    fd_private = fd_limits(PROTECTED_UNITS)
    (private_dir / "fd-limits.json").write_text(
        json.dumps(fd_private, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    psi_start = read_pressure()
    samples: list[dict[str, float]] = []
    start = datetime.now(UTC)

    proc = subprocess.Popen(
        ["vmstat", "-w", "1", str(duration + 1)],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    header: list[str] = []
    lines: list[str] = []
    for line in proc.stdout:
        if line.lstrip().startswith("procs"):
            header.append(line.rstrip())
            continue
        if len(line.split()) >= 10:
            lines.append(line)
        if len(lines) >= duration + 1:
            samples.append(
                {
                    "ts": time.time(),
                    "mem_available_mib": mem_available_mib(),
                    "memory_full_avg60": read_pressure()["memory"].get("full_avg60", 0.0),
                }
            )
            if len(samples) >= duration:
                break
    proc.wait(timeout=30)
    psi_end = read_pressure()

    (private_dir / "vmstat.txt").write_text("".join(header) + "".join(lines), encoding="utf-8")
    (private_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(item) for item in samples) + "\n", encoding="utf-8"
    )
    (private_dir / "pressure.json").write_text(
        json.dumps({"start": psi_start, "end": psi_end}, indent=2), encoding="utf-8"
    )

    rows = parse_vmstat(lines)
    # 第一条数据行是「自开机以来」的累计平均值，排除后才能按秒解释。
    if rows:
        rows = rows[1:]
    psi_for_gate = {
        "samples_full_avg60": [s["memory_full_avg60"] for s in samples],
    }
    summary = summarize(
        rows=rows,
        interval_seconds=min(duration, len(rows)),
        mem_min_mib=min((s["mem_available_mib"] for s in samples), default=0.0),
        psi=psi_for_gate,
    )
    verdict = evaluate(summary, gates)

    public = {
        "status": "preflight_passed" if verdict["passed"] else "preflight_blocked",
        "config_version": config["config_version"],
        "window": {
            "start": start.isoformat(),
            "end": datetime.now(UTC).isoformat(),
            "duration_seconds": summary["duration_seconds"],
        },
        "swap": {
            "in_nonzero_seconds": summary["swap_in_nonzero_seconds"],
            "out_nonzero_seconds": summary["swap_out_nonzero_seconds"],
            "in_kib": summary["swap_in_kib"],
            "out_kib": summary["swap_out_kib"],
            "max_in_nonzero_seconds_per_minute": summary[
                "max_swap_in_nonzero_seconds_per_minute"
            ],
        },
        "memory": {
            "available_min_mib": summary["mem_available_min_mib"],
            "psi_full_avg60_percent_max": summary["memory_psi_full_avg60_percent_max"],
        },
        "gates": gates,
        "checks": verdict["checks"],
        "passed": verdict["passed"],
        "minutes": summary["minutes"],
        "percentages": summary.get("percentages", {}),
        "new_model_calls": 0,
        "load_runs": 0,
        "note": "只读观测；未启动桩/负载、未改服务或系统配置、未调用模型。",
    }
    (public_dir / "summary.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (private_dir / "summary.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(public, indent=2, ensure_ascii=False))
    print(f"private={private_dir}", file=sys.stderr)
    print(f"public={public_dir}", file=sys.stderr)
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
