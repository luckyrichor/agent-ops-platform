"""Fixed labeled routing and outcome fixture. Measures decision correctness, not provider latency."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from opentelemetry.trace import get_tracer

from agent_ops.support import SupportRequest, SupportService, route_message


async def evaluate(path: Path) -> dict[str, object]:
    cases = json.loads(path.read_text())
    ids = [case["id"] for case in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError("invalid dataset")
    service = SupportService(get_tracer("support-eval"))
    results = []
    confusion: dict[str, dict[str, int]] = {}
    for case in cases:
        request = SupportRequest(message=case["message"], order_id=case.get("order_id"))
        route = route_message(request.message)
        ticket = await service.handle(request, case["id"])
        passed = route == case["route"] and ticket.state == case["state"]
        bucket = confusion.setdefault(case["route"], {})
        bucket[route] = bucket.get(route, 0) + 1
        results.append({"id": case["id"], "predicted": route, "expected": case["route"],
                        "state": ticket.state, "expected_state": case["state"], "passed": passed})
    return {"dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "cases": len(results), "passed": sum(bool(row["passed"]) for row in results),
            "confusion": confusion, "results": results,
            "method": "deterministic rule routing + in-memory fixture order tool; no live LLM"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, default=Path("evals/support-v1.json"))
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    result = asyncio.run(evaluate(args.dataset))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)
    if result["passed"] != result["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
