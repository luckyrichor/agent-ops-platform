import asyncio
import json

from agent_ops.agents import demo_agents
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import CommercePlanner, Dispatcher


async def main() -> None:
    request = CommerceRequest(request="帮我选预算 50 元、送上海的水壶", category="kettle",
                              budget_cent=5000, region="上海")
    result = await Dispatcher(demo_agents()).run(request, CommercePlanner().plan(request))
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
