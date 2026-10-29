"""P7 fixed business graph with actual catalog and customer-isolated memory."""
import hashlib
import json
from typing import cast

import httpx
from agent_memory.api.schemas import SearchRequest
from agent_memory.domain.enums import MemoryType, ScopeKind
from agent_memory.domain.models import MemoryScope
from agent_memory.sdk import MemoryAPIError

from agent_ops.agents import recommendation
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.customer_memory import CustomerMemory, CustomerSession
from agent_ops.models import CommerceRequest, RunResult
from agent_ops.orchestrator import AgentHandler, Dispatcher
from agent_ops.planner import Planner


class Shopping:
    def __init__(self, catalog: CatalogSearch, dispatcher: Dispatcher, planner: Planner,
                 memory: CustomerMemory | None) -> None:
        self.catalog, self.dispatcher, self.planner, self.memory = catalog, dispatcher, planner, memory

    def handlers(self) -> dict[str, AgentHandler]:
        async def catalog(body: CommerceRequest, _: dict[str, dict[str, object]]) -> dict[str, object]:
            items = await self.catalog.search(CatalogQuery(query=body.request,
                category_id=body.category_id, min_price_cent=body.min_price_cent,
                max_price_cent=min(body.budget_cent,2**63-1), attributes=body.attributes, limit=100))
            return {'items':items, 'source':'postgresql-catalog'}

        async def pricing(_: CommerceRequest, inputs: dict[str, dict[str, object]]) -> dict[str, object]:
            items = cast(list[dict[str, object]], inputs['catalog']['items'])
            return {'quotes':[{'id':str(p['id']), 'title':p['title'], 'price_cent':p['price_cents']} for p in items]}

        async def shipping(body: CommerceRequest, inputs: dict[str, dict[str, object]]) -> dict[str, object]:
            items = cast(list[dict[str, object]], inputs['catalog']['items'])
            return {'fees':{str(p['id']):p['freight_cents'] for p in items}, 'region':body.region}

        return {'catalog':catalog, 'pricing':pricing, 'shipping':shipping, 'recommendation':recommendation}

    async def graph(self, body: CommerceRequest) -> RunResult:
        # Preserve the existing tracer; no extra telemetry providers or demo handlers.
        graph = Dispatcher(self.handlers(), self.dispatcher.tracer)
        result = await graph.run(body, await self.planner.plan(body))
        result.request_id = str(body.run_id)
        result.planner_mode, result.planner_model = self.planner.mode, self.planner.model
        return result

    async def run(self, body: CommerceRequest, user_id: int) -> RunResult:
        if self.memory is None:
            return await self.graph(body)
        async with self.memory.session(user_id) as session:
            digest = hashlib.sha256(body.model_dump_json(exclude={'run_id'}).encode()).hexdigest()
            operation = await self.memory.operation(user_id, body.run_id, digest)
            if operation['state'] == 'succeeded':
                return RunResult.model_validate(operation['result'])
            return await self.with_memory(body, user_id, session, operation['memory_content'])

    async def with_memory(self, body: CommerceRequest, user_id: int,
                          session: CustomerSession, frozen_content: str | None) -> RunResult:
        assert self.memory is not None
        read_status, read_error, hits = 'available', None, 0
        effective = body
        try:
            if body.memory_id is not None:
                previous = await session.sdk.get(body.memory_id)
                content = json.loads(previous.content)
                budget = content.get('budget_cent') if isinstance(content,dict) else None
                if isinstance(budget,int) and not isinstance(budget,bool) and budget >= 0:
                    effective = body.model_copy(update={'budget_cent':min(body.budget_cent,budget)})
            else:
                page = await session.sdk.search(SearchRequest(query=body.request, limit=3))
                hits = len(page.items)
                if page.vector_status == 'degraded':
                    read_status = 'search_degraded'
        except (MemoryAPIError,httpx.HTTPError,ValueError):
            read_status, read_error = 'read_degraded', 'MEMORY_READ_FAILED'
        result = await self.graph(effective)
        result.memory_read_status, result.memory_read_error_code = read_status, read_error
        result.memory_status, result.memory_hit_count = read_status, hits
        result.memory_write_status = 'not_attempted'
        if result.outcome != 'succeeded':
            return result
        # Freeze the exact write body before the network call. Retrying after a
        # timeout cannot turn changed inventory into a same-key/different-body write.
        content = frozen_content or json.dumps({'schema_version':1, 'category':body.category,
            'budget_cent':effective.budget_cent, 'region':body.region,
            'recommendation':result.recommendation},sort_keys=True)
        if frozen_content is None:
            await self.memory.save_content(user_id,body.run_id,content)
        try:
            saved = await session.sdk.remember(content,MemoryType.EPISODIC,
                MemoryScope(ScopeKind.USER_GLOBAL,None,session.user),
                idempotency_key=f'commerce:v1:{session.user}:{body.run_id}:remember')
            result.memory_id, result.memory_write_status = saved.memory_id, 'succeeded'
            await self.memory.finish(user_id,body.run_id,result.model_dump(mode='json'))
        except MemoryAPIError as error:
            if error.status_code == 409:
                raise
            result.memory_write_status, result.memory_write_error_code = 'degraded','MEMORY_WRITE_FAILED'
        except (httpx.HTTPError,ValueError):
            result.memory_write_status, result.memory_write_error_code = 'degraded','MEMORY_WRITE_FAILED'
        if result.memory_write_status == 'degraded':
            result.memory_status = 'read_write_degraded' if read_status=='read_degraded' else 'write_degraded'
        return result
