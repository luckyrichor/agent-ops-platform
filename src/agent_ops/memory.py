"""Memory is an optional HTTP tool, with the caller's own bearer identity."""
import json

import httpx
from agent_memory.api.schemas import SearchRequest
from agent_memory.domain.enums import MemoryType, ScopeKind
from agent_memory.domain.models import MemoryScope
from agent_memory.sdk import MemoryAPIError, MemoryClient

from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import CommercePlanner, Dispatcher


class MemoryCommerce:
    def __init__(self, dispatcher: Dispatcher, client: MemoryClient | None) -> None:
        self.dispatcher = dispatcher
        self.client = client

    async def run(self, request: CommerceRequest) -> RunResult:
        status = "not_configured" if self.client is None else "available"
        effective = request
        hit_count = 0
        if self.client is not None and request.memory_id is not None:
            try:
                previous = await self.client.get(request.memory_id)
                preference = json.loads(previous.content)
                # Historical context can only narrow an explicit user's budget.
                budget = preference.get("budget_cent") if isinstance(preference, dict) else None
                if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
                    effective = request.model_copy(update={
                        "budget_cent": min(request.budget_cent, budget)})
            except (MemoryAPIError, httpx.HTTPError, ValueError):
                status = "read_degraded"
        elif self.client is not None:
            try:
                page = await self.client.search(SearchRequest(query=request.request,
                    memory_type=MemoryType.SEMANTIC, workspace_id="commerce", limit=3))
                hit_count = len(page.items)
                # Search results are historical context. Never turn arbitrary retrieved text
                # into executable tool instructions or overwrite the explicit user's request.
                if page.vector_status == "degraded":
                    status = "search_degraded"
            except (MemoryAPIError, httpx.HTTPError, ValueError):
                status = "read_degraded"
        result = await self.dispatcher.run(effective, CommercePlanner().plan(effective))
        result.request_id = str(request.run_id)
        result.memory_status = status
        result.memory_hit_count = hit_count
        if self.client is not None and result.outcome == "succeeded":
            try:
                saved = await self.client.remember(
                    json.dumps({"request": request.model_dump(mode="json"),
                                "budget_cent": effective.budget_cent,
                                "recommendation": result.recommendation}, sort_keys=True),
                    MemoryType.EPISODIC, MemoryScope(ScopeKind.WORKSPACE, "commerce", None),
                    idempotency_key=f"commerce:{request.run_id}")
                result.memory_id = saved.memory_id
            except MemoryAPIError as error:
                if error.status_code == 409:
                    raise
                result.memory_status = "write_degraded"
            except (httpx.HTTPError, ValueError):
                result.memory_status = "write_degraded"
        return result
