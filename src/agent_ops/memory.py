"""Memory is an optional HTTP tool, with the caller's own bearer identity."""
import json

import httpx
from agent_memory.api.schemas import SearchRequest
from agent_memory.domain.enums import MemoryType, ScopeKind
from agent_memory.domain.models import MemoryScope
from agent_memory.sdk import MemoryAPIError, MemoryClient
from opentelemetry.trace import Status, StatusCode

from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import DeterministicPlanner, Planner, PlanningError
from agent_ops.telemetry import hop


class MemoryCommerce:
    def __init__(self, dispatcher: Dispatcher, client: MemoryClient | None,
                 planner: Planner | None = None) -> None:
        self.dispatcher = dispatcher
        self.client = client
        self.planner = planner or DeterministicPlanner()

    async def run(self, request: CommerceRequest) -> RunResult:
        with hop(self.dispatcher.tracer, "commerce.request") as current:
            result = await self._run(request)
            context = current.get_span_context()
            result.trace_id = f"{context.trace_id:032x}" if context.is_valid else None
            result.trace_sampled = context.trace_flags.sampled
            current.set_attribute("outcome", result.outcome)
            current.set_attribute("memory_status", result.memory_status)
            current.set_attribute("memory_read_status", result.memory_read_status)
            current.set_attribute("memory_write_status", result.memory_write_status)
            if result.outcome != "succeeded":
                current.set_status(Status(StatusCode.ERROR))
            return result

    async def _run(self, request: CommerceRequest) -> RunResult:
        status = "not_configured" if self.client is None else "available"
        effective = request
        hit_count = 0
        read_error_code = None
        if self.client is not None and request.memory_id is not None:
            try:
                with hop(self.dispatcher.tracer, "tool.memory.get"):
                    previous = await self.client.get(request.memory_id)
                preference = json.loads(previous.content)
                # Historical context can only narrow an explicit user's budget.
                budget = preference.get("budget_cent") if isinstance(preference, dict) else None
                if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
                    effective = request.model_copy(update={
                        "budget_cent": min(request.budget_cent, budget)})
            except (MemoryAPIError, httpx.HTTPError, ValueError) as error:
                status = "read_degraded"
                read_error_code = (error.code if isinstance(error, MemoryAPIError)
                                   else "MEMORY_READ_FAILED")
        elif self.client is not None:
            try:
                with hop(self.dispatcher.tracer, "tool.memory.search"):
                    page = await self.client.search(SearchRequest(query=request.request,
                        memory_type=MemoryType.SEMANTIC, workspace_id="commerce", limit=3))
                hit_count = len(page.items)
                # Search results are historical context. Never turn arbitrary retrieved text
                # into executable tool instructions or overwrite the explicit user's request.
                if page.vector_status == "degraded":
                    status = "search_degraded"
            except (MemoryAPIError, httpx.HTTPError, ValueError) as error:
                status = "read_degraded"
                read_error_code = (error.code if isinstance(error, MemoryAPIError)
                                   else "MEMORY_READ_FAILED")
        if self.planner.mode == "llm":
            with hop(self.dispatcher.tracer, "planner.llm") as planning_span:
                planning_span.set_attribute("planner_mode", self.planner.mode)
                planning_span.set_attribute("planner_model", self.planner.model or "none")
                try:
                    tasks = await self.planner.plan(effective)
                except PlanningError as error:
                    planning_span.set_attribute("planner_error_code", error.code)
                    raise
        else:
            tasks = await self.planner.plan(effective)
        result = await self.dispatcher.run(effective, tasks)
        result.planner_mode = self.planner.mode
        result.planner_model = self.planner.model
        result.request_id = str(request.run_id)
        result.memory_status = status
        result.memory_read_status = status
        result.memory_read_error_code = read_error_code
        result.memory_write_status = "not_configured" if self.client is None else "not_attempted"
        result.memory_hit_count = hit_count
        if self.client is not None and result.outcome == "succeeded":
            try:
                with hop(self.dispatcher.tracer, "tool.memory.remember"):
                    saved = await self.client.remember(
                        json.dumps({"schema_version": 1, "category": request.category,
                                    "region": request.region,
                                    "budget_cent": effective.budget_cent,
                                    "recommendation": result.recommendation}, sort_keys=True),
                        MemoryType.EPISODIC, MemoryScope(ScopeKind.WORKSPACE, "commerce", None),
                        idempotency_key=f"commerce:{request.run_id}")
                result.memory_id = saved.memory_id
                result.memory_write_status = "succeeded"
            except MemoryAPIError as error:
                if error.status_code == 409:
                    raise
                result.memory_write_status = "degraded"
                result.memory_write_error_code = error.code
            except (httpx.HTTPError, ValueError):
                result.memory_write_status = "degraded"
                result.memory_write_error_code = "MEMORY_WRITE_FAILED"
        if result.memory_write_status == "degraded":
            result.memory_status = {"read_degraded": "read_write_degraded",
                                    "search_degraded": "search_write_degraded"}.get(
                                        status, "write_degraded")
        return result
