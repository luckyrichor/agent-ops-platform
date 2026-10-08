"""Three real-provider ASGI runs; no token, prompt or raw response in the report."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

from agent_ops.api import create_app
from agent_ops.models import CommerceRequest
from agent_ops.planner import planner_from_env


async def main() -> None:
    planner = planner_from_env()
    if planner.mode != "llm":
        raise ValueError("live verification requires AGENT_OPS_PLANNER=llm")
    cases = [
        ("in-budget", "kettle", 5000, "上海", "kettle-basic"),
        ("shipping-over-budget", "kettle", 4500, "杭州", None),
        ("empty-catalog", "unknown", 5000, "上海", None),
    ]
    results = []
    app = create_app(planner=planner)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        for name, category, budget, region, expected in cases:
            request = CommerceRequest(
                request="推荐一个预算内的商品", category=category, budget_cent=budget, region=region
            )
            start = perf_counter()
            reply = await api.post("/v1/commerce/runs", json=request.model_dump(mode="json"))
            if reply.status_code != 200:
                # HTTP detail is the application's fixed safe reason code.
                results.append(
                    {
                        "case": name,
                        "outcome": "blocked",
                        "http_status": reply.status_code,
                        "reason_code": reply.json().get("detail"),
                        "planner_model": planner.model,
                    }
                )
                break
            data = reply.json()
            selected = data["recommendation"]["selected"]
            assert data["outcome"] == "succeeded" and data["planner_mode"] == "llm"
            assert (selected["id"] if selected else None) == expected
            if selected:
                assert selected["total_cent"] <= budget
            results.append(
                {
                    "case": name,
                    "outcome": data["outcome"],
                    "planner_mode": data["planner_mode"],
                    "planner_model": data["planner_model"],
                    "tasks": len(data["tasks"]),
                    "selected": selected["id"] if selected else None,
                    "duration_ms": round((perf_counter() - start) * 1000, 2),
                }
            )
    hashes = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path("src/agent_ops").glob("*.py"))
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases": results,
        "source_sha256": hashes,
        "limits": "3 synthetic commerce cases; real LLM plans, local catalog/price/shipping tools; not production quality or performance",
    }
    destination = Path("docs/measurements/llm-live-2026-10-08.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    passed = sum(r["outcome"] == "succeeded" for r in results)
    print(
        json.dumps(
            {
                "passed": passed,
                "required": len(cases),
                "model": planner.model,
                "report": str(destination),
            }
        )
    )
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
