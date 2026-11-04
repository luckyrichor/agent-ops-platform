#!/usr/bin/env python3
"""S2/S3 阶段2：基线期间的并行主机与保护服务采样（只读）。

按固定间隔采样：MemAvailable、内存/IO PSI、oom_kill 累计、pgmajfault，
并对 GetAudio/GetVideo /health 各发少量只读 GET（负载期中段采样，
不是仅前后空闲一次）。不修改/重启任何保护服务。
中止条件：读取失败或 MemAvailable<2GiB 时停止并如实标注。
"""
import argparse
import json
import time
from pathlib import Path

import httpx

sys_guard = 2048  # MiB


def read_int(prefix: str) -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(prefix):
            return int(line.split()[1]) // 1024
    return -1


def read_psi(kind: str) -> dict:
    try:
        text = Path(f"/proc/pressure/{kind}").read_text()
        out = {}
        for line in text.splitlines():
            name, _, rest = line.partition(" ")
            fields = dict(item.split("=") for item in rest.split())
            out[name] = {k: float(v) for k, v in fields.items()}
        return out
    except OSError as error:
        return {"error": str(error)}


def vmstat_counters() -> dict:
    counters = {}
    for line in Path("/proc/vmstat").read_text().splitlines():
        key, _, value = line.partition(" ")
        counters[key] = int(value)
    return {k: counters.get(k, -1) for k in ("oom_kill", "pgmajfault", "pswpin", "pswpout")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--probes-per-cycle", type=int, default=3)
    parser.add_argument("--stop-file", default="", help="controller completion marker; checked between samples")
    parser.add_argument("--out", default=".local/s2/host-metrics-stage2.json")
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0 or args.probes_per_cycle <= 0:
        raise SystemExit("duration/interval/probes-per-cycle must be positive")
    samples = []
    aborted = None
    started_wall = time.time()
    started = time.monotonic()
    initial_oom = vmstat_counters()["oom_kill"]
    targets = {"getaudio": "http://127.0.0.1:8800/health",
               "getvideo": "http://127.0.0.1:8900/health"}
    with httpx.Client(timeout=3, trust_env=False) as client:
        while time.monotonic() - started < args.duration:
            if args.stop_file and Path(args.stop_file).exists():
                break
            avail = read_int("MemAvailable:")
            if avail < 0:
                aborted = "MemAvailable unavailable"
                break
            if avail >= 0 and avail < sys_guard:
                aborted = f"MemAvailable {avail}MiB < {sys_guard}MiB"
                break
            counters = vmstat_counters()
            if counters["oom_kill"] > initial_oom:
                aborted = "host oom_kill increased"
                break
            probes = {}
            for name, url in targets.items():
                latencies = []
                codes = {}
                errors = 0
                for _ in range(args.probes_per_cycle):
                    begin = time.perf_counter()
                    try:
                        response = client.get(url)
                        codes[response.status_code] = codes.get(response.status_code, 0) + 1
                        if response.status_code == 200:
                            latencies.append((time.perf_counter() - begin) * 1000)
                    except httpx.TransportError:
                        errors += 1
                    time.sleep(0.02)
                probes[name] = {"latencies_ms": [round(v, 3) for v in latencies],
                                "status_codes": codes, "transport_errors": errors}
            samples.append({"t_offset_s": round(time.monotonic() - started, 1),
                            "at_unix": time.time(),
                            "mem_available_mib": avail, "psi_memory": read_psi("memory"),
                            "psi_io": read_psi("io"), "vmstat": counters,
                            "protect_probes": probes})
            time.sleep(args.interval)
    result = {"started_at_beijing": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                                  time.gmtime(started_wall + 8 * 3600)),
              "started_at_unix": started_wall,
              "finished_at_unix": time.time(),
              "elapsed_s": time.monotonic() - started,
              "duration_s": args.duration, "interval_s": args.interval,
              "samples": samples, "sample_count": len(samples),
              "aborted": aborted,
              "note": ("只读采样；oom_kill/pswpin/pswpout/pgmajfault 为内核累计计数，"
                       "解释增量需对照起止；保护探针为 /health 只读，非业务 p95")}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print(f"ok: {args.out} samples={len(samples)} aborted={aborted}")
    if aborted is not None:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
