#!/usr/bin/env python3
"""只读保护服务探针：对指定保护服务测量只读 HTTP 入口延迟。

2026-11-03 实测入口状态：
- GetAudio API（system 服务，127.0.0.1:8800）GET /health → 200，只读可用。
- GetVideo API（system 服务，127.0.0.1:8900）GET /health → 200，只读可用。
- 顾客 memory 服务（user 服务 agent-ops-customer-memory，127.0.0.1:18886）
  常见只读路径（/health /healthz /ready /livez /）均 404 —— 当前没有可用
  只读 HTTP 入口；该服务不得虚构 p95，只能记录"无入口"。

本脚本只发 GET 只读请求，不 restart / 不写任何保护服务。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_ops.bench.stats import nearest_rank

TARGETS = {
    "getaudio": "http://127.0.0.1:8800/health",
    "getvideo": "http://127.0.0.1:8900/health",
    "customer_memory": "http://127.0.0.1:18886/health",
}


def probe(name: str, url: str, samples: int) -> dict[str, object]:
    latencies: list[float] = []
    status_codes: dict[int, int] = {}
    errors = 0
    with httpx.Client(timeout=3, trust_env=False) as client:
        for _ in range(samples):
            start = time.perf_counter()
            try:
                response = client.get(url)
                elapsed = (time.perf_counter() - start) * 1000
                status_codes[response.status_code] = status_codes.get(response.status_code, 0) + 1
                if response.status_code == 200:
                    latencies.append(elapsed)
            except httpx.TransportError:
                errors += 1
            time.sleep(0.02)
    if latencies:
        ordered = sorted(latencies)
        stats = {"p50_ms": round(nearest_rank(ordered, 50), 3),
                 "p95_ms": round(nearest_rank(ordered, 95), 3),
                 "quantile_method": "nearest-rank (agent_ops.bench.stats.nearest_rank)",
                 "successful_samples": len(ordered)}
    else:
        stats = None
    entry = {"samples": samples, "status_codes": status_codes, "transport_errors": errors,
             "latency": stats}
    if 404 in status_codes and not latencies:
        entry["verdict"] = "no_read_only_http_entry"
    elif latencies:
        entry["verdict"] = "ok"
    else:
        entry["verdict"] = "unreachable"
    return entry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if args.samples <= 0:
        raise SystemExit("--samples must be positive")
    result = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "note": ("只读 GET 探针；分位数 nearest-rank；空闲采样不证明负载期间无退化，"
                       "阶段2需并行前/中/后采样并记录资源中止条件，否则不写保护通过；"
                       "customer_memory 无只读入口时 p95=None，不虚构"),
              "targets": {name: probe(name, url, args.samples) for name, url in TARGETS.items()}}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    main()
