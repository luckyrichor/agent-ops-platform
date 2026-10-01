from fastapi import FastAPI

from agent_ops.agents import demo_agents
from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import CommercePlanner, Dispatcher


def create_app(dispatcher: Dispatcher | None = None) -> FastAPI:
    app = FastAPI(title="Agent Ops commerce orchestration", version="0.1.0")
    planner = CommercePlanner()
    dispatch = dispatcher or Dispatcher(demo_agents())

    @app.post("/v1/commerce/runs", response_model=RunResult)
    async def run(request: CommerceRequest) -> RunResult:
        return await dispatch.run(request, planner.plan(request))

    return app
