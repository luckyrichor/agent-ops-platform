import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from agent_memory.sdk import MemoryAPIError, MemoryClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import Planner, PlanningError, planner_from_env
from agent_ops.streaming import stream_run
from agent_ops.telemetry import hop


class RequestTraceMiddleware:
    """Keep one trace context through response headers and the entire SSE lifetime."""

    def __init__(self, app: ASGIApp, dispatcher: Dispatcher) -> None:
        self.app, self.dispatcher = app, dispatcher

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with hop(self.dispatcher.tracer, "commerce.http", kind=SpanKind.SERVER) as current:
            trace_id = f"{current.get_span_context().trace_id:032x}"
            scope.setdefault("state", {})["trace_id"] = trace_id

            response_started = False

            async def traced_send(message: Message) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    message["headers"] = list(message.get("headers", [])) + [
                        (b"x-trace-id", trace_id.encode())]
                    current.set_attribute("http.response.status_code", message["status"])
                    if message["status"] >= 500:
                        current.set_status(Status(StatusCode.ERROR))
                        current.set_attribute("reason_code", "HTTP_REJECTED")
                try:
                    await send(message)
                except OSError:
                    raise ClientDisconnect from None

            try:
                await self.app(scope, receive, traced_send)
            except ClientDisconnect:
                raise
            except Exception:
                current.set_status(Status(StatusCode.ERROR))
                current.set_attribute("reason_code", "RUN_FAILED")
                if response_started:
                    raise
                response = JSONResponse(status_code=500, content={
                    "detail": "RUN_FAILED", "trace_id": trace_id})
                await response(scope, receive, traced_send)


def create_app(dispatcher: Dispatcher | None = None, planner: Planner | None = None) -> FastAPI:
    plan = planner or planner_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await plan.aclose()

    app = FastAPI(title="Agent Ops commerce orchestration", version="0.1.0", lifespan=lifespan)
    dispatch = dispatcher or Dispatcher(demo_agents())

    app.add_middleware(RequestTraceMiddleware, dispatcher=dispatch)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, headers=error.headers,
                            content={"detail": error.detail,
                                     "trace_id": request.state.trace_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Preserve field/type/message structure without echoing untrusted input or ctx.
        details = [{key: value for key, value in item.items() if key in {"loc", "msg", "type"}}
                   for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": details,
                            "trace_id": request.state.trace_id})

    async def execute(body: CommerceRequest, request: Request) -> RunResult:
        url = os.environ.get("AGENT_MEMORY_URL")
        if not url:
            return await MemoryCommerce(dispatch, None, plan).run(body)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "memory integration requires caller bearer token")
        async with httpx.AsyncClient(base_url=url, timeout=2) as client:
            try:
                return await MemoryCommerce(dispatch, MemoryClient(
                    client, token=authorization[7:]), plan).run(body)
            except MemoryAPIError as error:
                raise HTTPException(error.status_code, error.code) from error

    @app.post("/v1/commerce/stream")
    async def stream(body: CommerceRequest, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            async for event in stream_run(lambda: execute(body, request),
                                          request.is_disconnected,
                                          trace_id=request.state.trace_id):
                yield event
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/v1/commerce/runs", response_model=RunResult)
    async def run(body: CommerceRequest, request: Request) -> RunResult:
        try:
            return await execute(body, request)
        except PlanningError as error:
            raise HTTPException(503, error.code) from None

    return app
