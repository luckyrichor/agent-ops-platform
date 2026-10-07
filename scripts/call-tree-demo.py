"""Export safe successful and failed local call trees for inspection."""
import asyncio
import json
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher


async def main() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    outcomes = []
    for fail in (False, True):
        handlers = demo_agents()
        if fail:
            async def failing(request, inputs):
                raise RuntimeError("redacted fixture failure")
            handlers["pricing"] = failing
        result = await MemoryCommerce(Dispatcher(handlers, provider.get_tracer("demo")), None).run(
            CommerceRequest(request="synthetic fixture", category="kettle", budget_cent=5000,
                            region="北京"))
        outcomes.append({"outcome": result.outcome, "trace_id": result.trace_id})
    path = Path("docs/measurements/w10-call-tree.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"outcomes": outcomes,
                               "spans": [json.loads(s.to_json()) for s in exporter.get_finished_spans()]},
                              indent=2)+"\n")
    provider.shutdown()
    print("2 traces exported, including pricing tool failure")


asyncio.run(main())
