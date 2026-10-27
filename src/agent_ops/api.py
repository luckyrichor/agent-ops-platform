import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_memory.sdk import MemoryAPIError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_ops.access_api import admin_router, auth_router, orders_router
from agent_ops.agents import demo_agents
from agent_ops.auth import (
    STAFF,
    AccountStore,
    AuthService,
    InMemoryAccountStore,
    Principal,
    TokenCodec,
    principal_dependency,
    role_guard,
)
from agent_ops.auth_postgres import PostgresAccountStore
from agent_ops.commerce_backend import CommerceBackend
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import Planner, PlanningError, planner_from_env
from agent_ops.streaming import stream_run
from agent_ops.support import SupportService
from agent_ops.support_api import SupportBackend, support_router
from agent_ops.support_postgres import PostgresSupportService
from agent_ops.telemetry import hop
from agent_ops.workbench import WorkbenchService
from agent_ops.workbench_api import workbench_router


class RequestTraceMiddleware:
    """Keep one trace context through response headers and the entire SSE lifetime."""

    def __init__(self, app: ASGIApp, dispatcher: Dispatcher) -> None:
        self.app, self.dispatcher = app, dispatcher

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        name = ("support.http" if path.startswith(("/support", "/workbench"))
                else "auth.http" if path.startswith(("/auth", "/admin")) else "commerce.http")
        with hop(self.dispatcher.tracer, name, kind=SpanKind.SERVER) as current:
            trace_id = f"{current.get_span_context().trace_id:032x}"
            scope.setdefault("state", {})["trace_id"] = trace_id
            sampled = current.get_span_context().trace_flags.sampled
            scope["state"]["trace_sampled"] = sampled

            response_started = False

            async def traced_send(message: Message) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    message["headers"] = list(message.get("headers", [])) + [
                        (b"x-trace-id", trace_id.encode()),
                        (b"x-trace-sampled", str(sampled).lower().encode())]
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
                    "detail": "RUN_FAILED", "trace_id": trace_id, "trace_sampled": sampled})
                await response(scope, receive, traced_send)


def create_app(dispatcher: Dispatcher | None = None, planner: Planner | None = None,
               *, baseline_without_auth: bool = False, legacy_support: bool = False,
               lifecycle_interval: float = 1) -> FastAPI:
    plan = planner or planner_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def lifecycle() -> None:
            while True:
                await asyncio.sleep(lifecycle_interval)
                if isinstance(support, WorkbenchService):
                    try:
                        await support.maintain()
                    except SQLAlchemyError:
                        logging.getLogger(__name__).warning('support lifecycle failed; retrying')
        task = asyncio.create_task(lifecycle()) if lifecycle_interval > 0 and isinstance(support, WorkbenchService) else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            try:
                await plan.aclose()
            finally:
                if isinstance(support, PostgresSupportService):
                    await support.aclose()
                if isinstance(store, PostgresAccountStore):
                    await store.aclose()
                if commerce is not None:
                    await commerce.aclose()
                if dispatcher is None:
                    await dispatch.aclose()

    app = FastAPI(title="Agent Ops commerce orchestration", version="0.1.0", lifespan=lifespan)
    dispatch = dispatcher or Dispatcher(demo_agents())
    backend = os.environ.get("AGENT_OPS_SUPPORT_BACKEND", "memory")
    support: SupportBackend
    store: AccountStore
    commerce: CommerceBackend | None = None
    secret = os.environ.get("AGENT_OPS_AUTH_SECRET", "")
    if backend == "postgres":
        if not secret:
            # Persistent accounts need a stable signing key shared by every worker.
            raise ValueError("AGENT_OPS_AUTH_SECRET is required with the postgres backend")
        url = os.environ["AGENT_OPS_DATABASE_URL"]
        support = (PostgresSupportService(url, dispatch.tracer) if legacy_support
                   else WorkbenchService(url, dispatch.tracer))
        store = PostgresAccountStore(url)
        commerce = CommerceBackend(url)
    elif backend == "memory":
        # Baseline switch: accounts and tokens live and die with this single process.
        support = SupportService(dispatch.tracer)
        store = InMemoryAccountStore()
        secret = secret or secrets.token_urlsafe(48)
    else:
        raise ValueError("unknown support backend")
    auth = AuthService(store, TokenCodec(secret))
    signed_in = principal_dependency(auth.codec)
    if baseline_without_auth:
        # Only the explicit loopback benchmark factory opts in; normal API always authenticates.
        async def benchmark_customer() -> Principal:
            return Principal(1, "customer", 1, "benchmark-only")
        signed_in = benchmark_customer
    customer = role_guard(signed_in, "customer")
    staff = role_guard(signed_in, *STAFF)
    app.state.support, app.state.auth, app.state.commerce = support, auth, commerce
    app.include_router(auth_router(auth, signed_in=signed_in))
    app.include_router(support_router(support, commerce, customer=customer, staff=staff,
                                      signed_in=signed_in, enable_resolve=not isinstance(support, WorkbenchService)))
    app.include_router(workbench_router(support if isinstance(support, WorkbenchService) else None, auth,
                                      customer=customer, staff=staff, admin=role_guard(signed_in, "admin"),
                                      signed_in=signed_in))
    app.include_router(orders_router(commerce, customer=customer))
    app.include_router(admin_router(auth, admin=role_guard(signed_in, "admin")))

    app.add_middleware(RequestTraceMiddleware, dispatcher=dispatch)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, headers=error.headers,
                            content={"detail": error.detail,
                                     "trace_id": request.state.trace_id,
                                     "trace_sampled": request.state.trace_sampled})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Preserve field/type/message structure without echoing untrusted input or ctx.
        details = [{key: value for key, value in item.items() if key in {"loc", "msg", "type"}}
                   for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": details,
                            "trace_id": request.state.trace_id,
                                     "trace_sampled": request.state.trace_sampled})

    async def execute(body: CommerceRequest, request: Request) -> RunResult:
        # agent-memory only accepts its own RS256 user JWT (UUID sub/tenant), not a
        # service credential + platform bigint user ID. P7 gap: never forward platform tokens.
        try:
            return await MemoryCommerce(dispatch, None, plan).run(body)
        except MemoryAPIError as error:
            raise HTTPException(error.status_code, error.code) from error

    @app.post("/v1/commerce/stream", dependencies=[Depends(customer)])
    async def stream(body: CommerceRequest, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            async for event in stream_run(lambda: execute(body, request),
                                          request.is_disconnected,
                                          trace_id=request.state.trace_id):
                yield event
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/v1/commerce/runs", response_model=RunResult, dependencies=[Depends(customer)])
    async def run(body: CommerceRequest, request: Request) -> RunResult:
        try:
            return await execute(body, request)
        except PlanningError as error:
            raise HTTPException(503, error.code) from None

    return app
