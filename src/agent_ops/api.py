import os
from collections.abc import AsyncIterator

import httpx
from agent_memory.sdk import MemoryAPIError, MemoryClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import Dispatcher
from agent_ops.streaming import stream_run


def create_app(dispatcher: Dispatcher | None = None) -> FastAPI:
    app = FastAPI(title="Agent Ops commerce orchestration", version="0.1.0")
    dispatch = dispatcher or Dispatcher(demo_agents())

    async def execute(body: CommerceRequest, request: Request) -> RunResult:
        url = os.environ.get("AGENT_MEMORY_URL")
        if not url:
            return await MemoryCommerce(dispatch, None).run(body)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "memory integration requires caller bearer token")
        async with httpx.AsyncClient(base_url=url, timeout=2) as client:
            try:
                return await MemoryCommerce(dispatch, MemoryClient(
                    client, token=authorization[7:])).run(body)
            except MemoryAPIError as error:
                raise HTTPException(error.status_code, error.code) from error

    @app.post("/v1/commerce/stream")
    async def stream(body: CommerceRequest, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            async for event in stream_run(lambda: execute(body, request),
                                          request.is_disconnected):
                yield event
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/v1/commerce/runs", response_model=RunResult)
    async def run(body: CommerceRequest, request: Request) -> RunResult:
        return await execute(body, request)

    return app
