#!/usr/bin/env python3
"""P9 模型桩校准：真实 DeepSeek 编排 profile vs 本地桩，独立留出样本。

- 只用纯 P2 合成请求（模板生成，不读 ECD、不读 P8 最终集）
- 真实调用并发 ≤2、每请求最多 3 次、总计含重试 ≤1200 次
- 客户端所见完整调用延迟；拟合用 500、验证用独立 500
- 判定口径来自 acceptance-v1.json，跑前固定
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_ops.bench import config, stats
from agent_ops.bench.faults import FaultProfile
from agent_ops.bench.stub import LatencyModel, StubServer
from agent_ops.planner import SYSTEM

CATEGORIES = ["electronics", "home", "outdoors", "office", "kitchen", "sports"]
REGIONS = ["north", "south", "east", "west", "central"]
TEMPLATES = [
    "I need a durable product for daily use under {budget} cents, prefer small size",
    "Find me something suitable for a gift, category {category}, budget around {budget}",
    "Compare a few options and recommend one within {budget} cents, ship to {region}",
    "Looking for a replacement for a broken item; keep total cost below {budget} cents",
    "Recommend the best value item for {region} delivery, spending at most {budget} cents",
]


def synthetic_body(identifier: int, rng: random.Random) -> dict[str, object]:
    category = CATEGORIES[identifier % len(CATEGORIES)]
    region = REGIONS[identifier % len(REGIONS)]
    budget = 1500 + (identifier * 137) % 9000
    filler = " ".join(
        rng.choice(["lightweight", "portable", "reliable", "affordable", "compact"])
        for _ in range(6 + identifier % 18)
    )
    text = TEMPLATES[identifier % len(TEMPLATES)].format(
        budget=budget, category=category, region=region
    )
    return {
        "request": f"{text}. Details: {filler}",
        "category": category,
        "budget_cent": budget,
        "region": region,
    }


def body_for(identifier: int) -> dict[str, object]:
    rng = random.Random(identifier * 7919 + 13)
    commerce = synthetic_body(identifier, rng)
    return {
        "model": os.environ.get("DEEPSEEK_BENCH_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(commerce, ensure_ascii=False)},
        ],
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }


class CallBudget:
    def __init__(self, cap: int) -> None:
        self.cap, self.used = cap, 0
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        async with self.lock:
            if self.used >= self.cap:
                raise RuntimeError("REAL_CALL_CAP_EXCEEDED")
            self.used += 1


async def call_real(client: httpx.AsyncClient, endpoint: str, key: str, identifier: str,
                    body: dict[str, object], budget: CallBudget, attempts: int = 3) -> dict[str, object]:
    started = time.perf_counter()
    last_error = ""
    for attempt in range(attempts):
        await budget.take()
        try:
            response = await client.post(
                endpoint,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                json=body,
            )
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                payload = response.json()
                usage = payload.get("usage", {})
                return {
                    "id": identifier,
                    "ok": True,
                    "latency_ms": elapsed,
                    "attempts": attempt + 1,
                    "usage": usage,
                    "returned_model": payload.get("model", ""),
                }
            last_error = f"http_{response.status_code}"
            if response.status_code not in (408, 429) and response.status_code < 500:
                break
        except httpx.HTTPError as error:
            last_error = type(error).__name__
        if attempt + 1 < attempts:
            await asyncio.sleep(1.0 * (attempt + 1))
    return {"id": identifier, "ok": False, "latency_ms": 0.0, "error": last_error,
            "attempts": attempts}


async def measure_real(config_path: Path, identifiers: list[str], out: Path) -> list[dict[str, object]]:
    cfg = config.load(config_path)
    calibration = cfg.calibration
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    endpoint = os.environ.get("DEEPSEEK_BENCH_ENDPOINT", "https://api.deepseek.com/chat/completions")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is required for authorized calibration")
    budget = CallBudget(int(calibration["real_calls_cap_including_retries"]))
    semaphore = asyncio.Semaphore(int(calibration["concurrency_max"]))
    results: list[dict[str, object]] = []

    async with httpx.AsyncClient(timeout=40, trust_env=False, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=4)) as client:
        async def one(identifier: str) -> None:
            async with semaphore:
                row = await call_real(client, endpoint, key, identifier,
                                      body_for(int(identifier.split("-")[-1])), budget,
                                      int(calibration["attempts_per_request_max"]))
                row["phase"] = "fit" if identifier.startswith("fit") else "validate"
                results.append(row)

        await asyncio.gather(*(one(identifier) for identifier in identifiers))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


async def measure_stub(samples_ms: list[float], identifiers: list[str], overhead_ms: float,
                       seed: int) -> list[dict[str, object]]:
    stub = StubServer(latency=LatencyModel(samples_ms, seed=seed, overhead_ms=overhead_ms),
                      profile=FaultProfile(seed=seed))
    await stub.start()
    rows: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        for identifier in identifiers:
            started = time.perf_counter()
            response = await client.post(
                stub.endpoint,
                headers={"X-Bench-Request-Id": identifier},
                json=body_for(int(identifier.split("-")[-1])),
            )
            elapsed = (time.perf_counter() - started) * 1000
            rows.append({"id": identifier, "ok": response.status_code == 200,
                         "latency_ms": elapsed, "status": response.status_code})
    await stub.stop()
    return rows


async def measure_overhead(seed: int, count: int = 30) -> float:
    stub = StubServer(latency=LatencyModel([0.0], seed=seed, overhead_ms=0.0),
                      profile=FaultProfile(seed=seed))
    await stub.start()
    samples: list[float] = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for index in range(count):
            started = time.perf_counter()
            await client.post(stub.endpoint, headers={"X-Bench-Request-Id": f"overhead-{index}"},
                              json={"probe": True})
            samples.append((time.perf_counter() - started) * 1000)
    await stub.stop()
    return stats.percentile(samples, 50)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=config.CONFIG_PATH)
    parser.add_argument("--fit", type=int, default=0)
    parser.add_argument("--validate", type=int, default=0)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "reports" / "p9-calibration")
    parser.add_argument("--private", type=Path, default=ROOT / ".local" / "p9-calibration")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reuse-real", type=Path, default=None)
    args = parser.parse_args()

    cfg = config.load(args.config)
    calibration = cfg.calibration
    fit_count = args.fit or int(calibration["fit_samples_min"])
    validate_count = args.validate or int(calibration["validation_samples_min"])
    if fit_count < int(calibration["fit_samples_min"]):
        raise SystemExit("fit sample below frozen minimum")
    if validate_count < int(calibration["validation_samples_min"]):
        raise SystemExit("validation sample below frozen minimum")
    if fit_count + validate_count > int(calibration["real_calls_cap_including_retries"]):
        raise SystemExit("planned calls exceed frozen cap")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    private_dir = args.private / stamp
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_dir = args.out / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    identifiers = [f"fit-{index:04d}" for index in range(1, fit_count + 1)] + [
        f"validate-{index:04d}" for index in range(1, validate_count + 1)
    ]
    if args.dry_run:
        print(json.dumps({"planned": len(identifiers), "fit": fit_count,
                          "validate": validate_count, "calls_cap":
                          calibration["real_calls_cap_including_retries"]}))
        return 0

    real_path = private_dir / "real.json"
    if args.reuse_real is not None:
        real_rows = json.loads(args.reuse_real.read_text(encoding="utf-8"))
    else:
        real_rows = asyncio.run(measure_real(args.config, identifiers, real_path))

    fit = [row for row in real_rows if row["phase"] == "fit" and row["ok"]]
    validated = [row for row in real_rows if row["phase"] == "validate" and row["ok"]]
    usage_tokens = sum(
        int(row.get("usage", {}).get("prompt_tokens", 0))
        + int(row.get("usage", {}).get("completion_tokens", 0))
        for row in real_rows if row["ok"]
    )
    if len(fit) < fit_count * 0.9 or len(validated) < validate_count * 0.9:
        summary = {
            "status": "calibration_incomplete",
            "fit_valid": len(fit), "validation_valid": len(validated),
            "planned": len(identifiers), "tokens": usage_tokens,
            "reason": "INSUFFICIENT_VALID_SAMPLES",
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary))
        return 2

    overhead_ms = asyncio.run(measure_overhead(int(calibration["bootstrap_seed"])))
    fit_latency = [float(row["latency_ms"]) for row in fit]
    real_validation = [float(row["latency_ms"]) for row in validated]
    stub_rows = asyncio.run(
        measure_stub(fit_latency, [str(row["id"]) for row in validated], overhead_ms,
                     int(calibration["bootstrap_seed"]))
    )
    stub_validation = [float(row["latency_ms"]) for row in stub_rows if row["ok"]]

    resamples = int(calibration["bootstrap_resamples"])
    seed = int(calibration["bootstrap_seed"])
    p50 = stats.bootstrap_ratio_ci(stub_validation, real_validation, q=50, resamples=resamples, seed=seed)
    p95 = stats.bootstrap_ratio_ci(stub_validation, real_validation, q=95, resamples=resamples, seed=seed)
    distance = stats.cdf_max_distance(stub_validation, real_validation)
    checks = {
        "p50_ratio_ci": stats.within(p50, calibration["p50_ratio_ci95"]),
        "p95_ratio_ci": stats.within(p95, calibration["p95_ratio_ci95"]),
        "cdf_distance": distance <= float(calibration["cdf_max_distance"]),
        "overhead_measured": overhead_ms <= float(calibration["model_stub_overhead_http_ms_max"]),
    }
    summary = {
        "status": "calibration_passed" if all(checks.values()) else "calibration_failed",
        "config_version": cfg.raw["config_version"],
        "fit_samples": len(fit_latency),
        "validation_samples": len(real_validation),
        "planned_calls": len(identifiers),
        "real_http_attempts": len(real_rows),
        "tokens": usage_tokens,
        "stub_http_overhead_ms": round(overhead_ms, 3),
        "real_latency_ms": {
            "fit": stats.summarize(fit_latency),
            "validation": stats.summarize(real_validation),
        },
        "stub_latency_ms": stats.summarize(stub_validation),
        "p50_ratio": p50,
        "p95_ratio": p95,
        "cdf_max_distance": distance,
        "checks": checks,
        "passed": all(checks.values()),
        "billing_verified": False,
        "note": "延迟拟合自真实调用，验证用独立样本；桩不代理真实 API；费用未核对账单。",
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    (private_dir / "fit_latency_ms.json").write_text(json.dumps(sorted(fit_latency)), encoding="utf-8")
    (private_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
