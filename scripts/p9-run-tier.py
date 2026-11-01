#!/usr/bin/env python3
"""P9 单次运行：桩 + 隔离 bench API + 看门狗 + Mac 生成器，产出证据 JSON。

一次只跑一格（场景 × 连接数 × 轮次）。资源预算、硬闸与判定口径来自 acceptance-v1.json。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_ops.bench import config, schema
from agent_ops.bench.watchdog import Watchdog

SUT_ADDR = os.environ.get("P9_SUT_ADDR", "100.87.80.92")
MAC = os.environ.get("P9_GENERATOR_HOST", "luomac")
MAC_PY = os.environ.get("P9_GENERATOR_PYTHON", "python3")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def load_application_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".local" / "application.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def bench_env(stub_port: int, base: dict[str, str]) -> dict[str, str]:
    url = base["AGENT_OPS_DATABASE_URL"]
    bench_url = url.rsplit("/", 1)[0] + "/agent_ops_bench"
    env = {
        **os.environ,
        "AGENT_OPS_SUPPORT_BACKEND": "postgres",
        "AGENT_OPS_DATABASE_URL": bench_url,
        "AGENT_OPS_AUTH_SECRET": base["AGENT_OPS_AUTH_SECRET"],
        "AGENT_OPS_BENCH_STUB_ENDPOINT": f"http://127.0.0.1:{stub_port}/v1/chat/completions",
        "AGENT_OPS_BENCH_AUTH": "off",
        "AGENT_OPS_PLANNER": "deterministic",
        "AGENT_OPS_TRACE_EXPORTER": "none",
        "OTEL_SDK_DISABLED": "true",
    }
    env.pop("AGENT_OPS_CUSTOMER_MEMORY_URL", None)
    return env


def wait_http(url: str, timeout: float = 30, *, stream: bool = False) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if stream:
                with (httpx.Client(timeout=5, trust_env=False) as client,
                      client.stream("GET", url) as response):
                    if response.status_code == 200:
                        for _ in response.iter_lines():
                            return True
            else:
                response = httpx.get(url, timeout=2, trust_env=False)
                if response.status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def rss_mib(pid: int) -> float:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0.0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


async def sample_resources(pids: list[int], stop: asyncio.Event, sink: dict[str, float]) -> None:
    peak = 0.0
    while not stop.is_set():
        total = sum(rss_mib(pid) for pid in pids)
        peak = max(peak, total)
        sink["peak_process_rss_mib"] = round(peak, 2)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            continue


def run_generator(args: argparse.Namespace, port: int, timings: dict[str, int],
                  private: Path) -> dict[str, object]:
    remote_json = f"/tmp/p9-run-{args.run_index}-{args.connections}.json"
    local_json = private / "generator.json"
    if args.generator == "local":
        command = [
            sys.executable, str(ROOT / "src" / "agent_ops" / "bench" / "loadgen.py"),
            "--host", SUT_ADDR, "--port", str(port), "--kind", args.kind,
            "--connections", str(args.connections), "--warmup", str(timings["warmup"]),
            "--ramp", str(timings["ramp"]), "--steady", str(timings["steady"]),
            "--drain", str(timings["drain"]), "--slow-ratio", str(args.slow_ratio),
            "--seed", str(args.seed), "--out", str(local_json), "--rows",
        ]
        subprocess.run(command, check=True, timeout=timings["hold"] + 300)
        return json.loads(local_json.read_text(encoding="utf-8"))

    scp = ["scp", "-q", str(ROOT / "src" / "agent_ops" / "bench" / "loadgen.py"),
           f"{MAC}:/tmp/p9-loadgen.py"]
    subprocess.run(scp, check=True, timeout=120)
    remote = [
        MAC_PY, "/tmp/p9-loadgen.py", "--host", SUT_ADDR, "--port", str(port),
        "--kind", args.kind, "--connections", str(args.connections),
        "--warmup", str(timings["warmup"]), "--ramp", str(timings["ramp"]),
        "--steady", str(timings["steady"]), "--drain", str(timings["drain"]),
        "--slow-ratio", str(args.slow_ratio), "--seed", str(args.seed),
        "--out", remote_json, "--rows",
    ]
    quoted = " ".join(f"'{item}'" for item in remote)
    subprocess.run(["ssh", "-o", "BatchMode=yes", MAC, quoted], check=True,
                   timeout=timings["hold"] + 300)
    subprocess.run(["scp", "-q", f"{MAC}:{remote_json}", str(local_json)], check=True, timeout=120)
    subprocess.run(["ssh", "-o", "BatchMode=yes", MAC, f"rm -f {remote_json}"], check=False)
    return json.loads(local_json.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connections", type=int, required=True)
    parser.add_argument("--kind", choices=("business", "transport"), default="business")
    parser.add_argument("--run-index", type=int, required=True)
    parser.add_argument("--slow-ratio", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--generator", choices=("local", "mac"), default="mac")
    parser.add_argument("--calibration", type=Path,
                        default=ROOT / ".local" / "p9-calibration")
    parser.add_argument("--public", type=Path, default=ROOT / "docs" / "reports" / "p9-runs")
    parser.add_argument("--private", type=Path, default=ROOT / ".local" / "p9-runs")
    parser.add_argument("--experiment-id", default="")
    args = parser.parse_args()

    cfg = config.load()
    tiers = cfg.tiers
    if args.connections not in tiers["allowed_connections"] + tiers["optional_connections"]:
        raise SystemExit("connection count not approved")
    timings = config.tier_seconds(tiers)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    experiment = args.experiment_id or f"p9-{stamp}"
    private = args.private / f"{experiment}-{args.kind}-{args.connections}-r{args.run_index}"
    private.mkdir(parents=True, exist_ok=False)
    public_dir = args.public / f"{experiment}-{args.kind}-{args.connections}-r{args.run_index}"
    public_dir.mkdir(parents=True, exist_ok=False)

    calibration_dirs = sorted(args.calibration.glob("*/fit_latency_ms.json"))
    if not calibration_dirs:
        raise SystemExit("calibration fit samples missing")
    latency_file = calibration_dirs[-1]
    base_env = load_application_env()

    stub_port, bench_port = free_port(), free_port()
    stub_log = (private / "stub.log").open("w")
    bench_log = (private / "bench.log").open("w")
    stub = subprocess.Popen(
        [sys.executable, "-m", "agent_ops.bench.stub", "--latency", str(latency_file),
         "--port", str(stub_port)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=stub_log, stderr=subprocess.STDOUT,
    )
    bench = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent_ops.bench.app:bench_app",
         "--host", SUT_ADDR, "--port", str(bench_port), "--log-level", "warning"],
        cwd=ROOT, env=bench_env(stub_port, base_env),
        stdout=bench_log, stderr=subprocess.STDOUT,
    )

    started_at = datetime.now(UTC).isoformat()
    watchdog = Watchdog(ROOT / ".local" / "p9-cgroup", mem_floor_mib=float(
        cfg.budget["host_mem_available_floor_mib"]))
    stop = asyncio.Event()
    resource_sink: dict[str, float] = {}
    try:
        if not wait_http(f"http://127.0.0.1:{stub_port}/healthz", timeout=30):
            raise SystemExit("stub did not become healthy")
        if not wait_http(f"http://{SUT_ADDR}:{bench_port}/bench/hold?seconds=0.2",
                         timeout=60, stream=True):
            raise SystemExit("bench app did not become healthy")

        async def orchestrate() -> dict[str, object]:
            watchdog_task = asyncio.create_task(watchdog.run(stop))
            resource_task = asyncio.create_task(
                sample_resources([bench.pid, stub.pid], stop, resource_sink))
            await asyncio.sleep(float(tiers["watchdog_background_seconds"]))
            if watchdog.aborted:
                stop.set()
            else:
                generated = await asyncio.to_thread(
                    run_generator, args, bench_port, timings, private)
            stop.set()
            await watchdog_task
            await resource_task
            if watchdog.aborted:
                return {}
            return generated

        generator_doc = asyncio.run(orchestrate())
    finally:
        stop.set()
        for process in (bench, stub):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        stub_log.close()
        bench_log.close()

    achieved = int(generator_doc["achieved_connections"])
    healthy = int(generator_doc["healthy_connections"])
    aborted = watchdog.aborted
    status = "completed" if achieved >= args.connections and healthy >= args.connections and not aborted else (
        "aborted" if aborted else "partial"
    )
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=False).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                capture_output=True, text=True, check=False).stdout.strip())
    record = {
        "schema_version": schema.SCHEMA_VERSION,
        "experiment_id": experiment,
        "run_index": args.run_index,
        "scenario": {
            "kind": args.kind,
            "connections": args.connections,
            "slow_reader_ratio": args.slow_ratio,
            "seed": args.seed,
            "timings": timings,
            "profile": "deepseek-stub: empirical CDF from authorized fit samples",
        },
        "source": {"commit": commit, "dirty": dirty,
                   "generator": "mac" if args.generator == "mac" else "local"},
        "started_at": started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "status": status,
        "target_connections": args.connections,
        "achieved_connections": min(achieved, args.connections),
        "healthy_connections": min(healthy, args.connections),
        "latency_ms": {
            "connect": generator_doc["latency_ms"]["connect"],
            "ttfb": generator_doc["latency_ms"]["ttfb"],
            "first_event": generator_doc["latency_ms"]["first_event"],
            "full": generator_doc["latency_ms"]["full"],
        },
        "errors": generator_doc["errors"],
        "throughput": generator_doc["throughput"],
        "resources": {
            "peak_memory_mib": resource_sink.get("peak_process_rss_mib", 0.0),
            "host_mem_available_min_mib": watchdog.summary()["min_mem_available_mib"],
            "watchdog_samples": watchdog.summary()["samples"],
        },
        "protection": {"aborted": aborted, "reasons": watchdog.reasons,
                       "memory_psi_full_avg60_max": watchdog.summary()["max_memory_psi_full_avg60"]},
        "completed_requests": generator_doc["completed_requests"],
        "frames_total": generator_doc["frames_total"],
    }
    schema.write_once(public_dir / "run.json", record, public=True)
    rows = generator_doc.get("rows") or []
    (private / "rows.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    (private / "generator.json").write_text(json.dumps(generator_doc, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    (private / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (private / "watchdog.json").write_text(json.dumps(watchdog.samples), encoding="utf-8")
    print(json.dumps({key: record[key] for key in
                      ("status", "target_connections", "achieved_connections",
                       "healthy_connections", "throughput", "errors", "protection")},
                     ensure_ascii=False))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
