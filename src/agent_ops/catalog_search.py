"""PostgreSQL FTS + structured filters. The cache seam currently passes through."""
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class CatalogQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    query: str = Field(default='', max_length=2000)
    category_id: int | None = Field(default=None, gt=0, le=2**63-1)
    min_price_cent: int = Field(default=0, ge=0, le=2**63-1)
    max_price_cent: int = Field(default=2**63-1, ge=0, le=2**63-1)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=20)
    available_only: bool = True
    limit: int = Field(default=24, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)


class CatalogCache:
    """Interface seam only; both modes fetch live data until the S3 experiments."""
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    async def load(self, search: 'CatalogSearch', query: CatalogQuery) -> list[dict[str, Any]]:
        return await search.database_query(query)


class CatalogSearch:
    def __init__(self, url: str, cache_enabled: bool = False) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.cache = CatalogCache(cache_enabled)

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def search(self, query: CatalogQuery) -> list[dict[str, Any]]:
        if not self.cache.enabled:
            return await self.database_query(query)
        return await self.cache.load(self, query)

    async def database_query(self, query: CatalogQuery) -> list[dict[str, Any]]:
        if query.min_price_cent > query.max_price_cent:
            return []
        async with self.engine.connect() as c:
            rows = await c.execute(text('''SELECT p.id,p.sku,p.title,p.category_id,p.price_cents,
                p.freight_cents,p.attrs,i.stock-i.reserved AS available,i.version AS inventory_version
              FROM commerce.products p JOIN commerce.inventory i ON p.id=i.product_id
              WHERE p.status='active' AND p.price_cents BETWEEN :low AND :high
                AND (:category=0 OR p.category_id=:category)
                AND p.attrs @> CAST(:attributes AS jsonb)
                AND (NOT :available_only OR i.stock-i.reserved>0)
                AND (:empty OR p.search_document @@ plainto_tsquery('english',:query))
              ORDER BY ts_rank_cd(p.search_document,plainto_tsquery('english',:query)) DESC,
                p.price_cents+p.freight_cents,p.id LIMIT :limit OFFSET :offset'''),
                {'low':query.min_price_cent,'high':query.max_price_cent,
                 'category':query.category_id or 0,'attributes':json.dumps(query.attributes),
                 'available_only':query.available_only,'empty':not query.query.strip(),
                 'query':query.query,'limit':query.limit,'offset':query.offset})
            return [dict(r) for r in rows.mappings()]
