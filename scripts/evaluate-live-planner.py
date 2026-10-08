"""Repeated real plans; fixed safe metadata only, single attempt exposes 429 samples."""
import asyncio
import hashlib
import json
import math
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

from agent_ops.models import CommerceRequest
from agent_ops.planner import DeterministicPlanner, LLMConfig, LLMPlanner, PlanningError


def percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {f"p{p}": round(ordered[max(0, math.ceil(len(ordered)*p/100)-1)], 3)
            for p in (50, 95, 99)} if ordered else {}


def git_state() -> tuple[str, bool]:
    return (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)))


async def main() -> None:
    base_commit, source_dirty = await asyncio.to_thread(git_state)
    statuses: Counter[int] = Counter()

    async def observe(response: httpx.Response) -> None:
        statuses[response.status_code] += 1

    config = LLMConfig(
        token=os.environ.get("ARK_API_KEY", ""),
        model=os.environ.get("AGENT_OPS_LLM_MODEL", LLMConfig.model),
        endpoint=os.environ.get("AGENT_OPS_LLM_ENDPOINT", LLMConfig.endpoint),
        timeout_seconds=15, concurrency=1, max_attempts=1,
    )
    samples: list[dict[str, object]] = []
    fixed = DeterministicPlanner()
    cases = [("in-budget", "kettle", 5000, "上海"),
             ("shipping-over-budget", "kettle", 4500, "杭州"),
             ("empty-catalog", "unknown", 5000, "上海")]
    async with httpx.AsyncClient(trust_env=False, timeout=15,
                                event_hooks={"response": [observe]}) as client:
        planner = LLMPlanner(config, client)
        for repeat in range(10):
            for name, category, budget, region in cases:
                request = CommerceRequest(request="推荐一个预算内的商品", category=category,
                                          budget_cent=budget, region=region)
                start = perf_counter()
                expected = await fixed.plan(request)
                deterministic_ms = (perf_counter()-start)*1000
                start = perf_counter()
                reason = "OK"
                try:
                    tasks = await planner.plan(request)
                    assert {t.task_id: set(t.depends_on) for t in tasks} == {
                        t.task_id: set(t.depends_on) for t in expected}
                except PlanningError as error:
                    reason = error.code
                samples.append({"case": name, "repeat": repeat, "reason_code": reason,
                                    "duration_ms": round((perf_counter()-start)*1000, 3),
                                    "deterministic_ms": round(deterministic_ms, 6)})
                print(f"sample {len(samples)}/30: {reason}", flush=True)
    counts = Counter(str(s['reason_code']) for s in samples)
    success = [float(s['duration_ms']) for s in samples if s['reason_code'] == 'OK']
    report = {"generated_at": datetime.now(UTC).isoformat(), "model": config.model,
                  "repetitions_per_case": 10, "total": 30, "max_attempts": 1, "concurrency": 1,
                  "outcomes": dict(counts), "valid_plan_fraction": counts['OK']/30,
                  "invalid_plan_fraction": counts['LLM_INVALID_PLAN']/30,
                  "observed_http_status_counts": dict(statuses),
                  "observed_429_fraction": statuses[429]/30,
                  "latency_all_ms": percentiles([float(s['duration_ms']) for s in samples]),
                  "latency_valid_ms": percentiles(success),
                  "latency_deterministic_ms": percentiles([float(s['deterministic_ms']) for s in samples]),
                  "samples": samples,
                  "base_commit": base_commit,
                  "source_dirty": source_dirty,
                  "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(Path('src/agent_ops').glob('*.py'))},
                  "limits": '30 sequential synthetic plans, 3 cases x10; no retry, no tools/memory; '
                         'nearest-rank percentiles, p99 effectively max; not free planning quality '
                         'or long-term availability/quota estimate; no prompt or raw response stored'}
    Path('docs/measurements/llm-repeated-2026-10-08.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('total','outcomes','latency_all_ms','observed_429_fraction')}))


if __name__ == '__main__':
    asyncio.run(main())
