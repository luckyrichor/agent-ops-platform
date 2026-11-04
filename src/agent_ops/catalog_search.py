"""PostgreSQL FTS + structured filters, with product-path Chinese retrieval (S3-a).

Chinese queries (CJK characters) route to a Chinese branch that matches
title_zh / description_zh (migration 0011, pg_trgm GIN) via zh_catalog.segment
longest-match terms ANDed together; English/price/inventory/category/attribute
filter semantics are unchanged. The cache seam currently passes through.
"""
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_ops.zh_catalog import segment

_CJK = re.compile(r'[\u4e00-\u9fff]')


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
        self._zh_ready: bool | None = None

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def search(self, query: CatalogQuery) -> list[dict[str, Any]]:
        if not self.cache.enabled:
            return await self.database_query(query)
        return await self.cache.load(self, query)

    async def _ensure_zh_columns(self) -> None:
        """Fail loudly when migration 0011 has not been applied; never silently drop."""
        if self._zh_ready is None:
            async with self.engine.connect() as c:
                present: int = (await c.execute(text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='commerce' AND table_name='products' "
                    "AND column_name IN ('title_zh','description_zh')"))).scalar_one()
            if present != 2:
                raise RuntimeError(
                    "Chinese catalog columns missing: apply migration 0011_chinese_catalog "
                    "(alembic upgrade head) before Chinese queries on CatalogSearch")
            self._zh_ready = True

    async def database_query(self, query: CatalogQuery) -> list[dict[str, Any]]:
        if query.min_price_cent > query.max_price_cent:
            return []
        if _CJK.search(query.query):
            await self._ensure_zh_columns()
            return await self._database_query_zh(query)
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

    async def _database_query_zh(self, query: CatalogQuery) -> list[dict[str, Any]]:
        """Product-path Chinese retrieval: same structured filters as the English path.

        Terms from zh_catalog.segment are ANDed across terms, OR-matched between
        title_zh and description_zh (both pg_trgm GIN indexed, migration 0011).
        """
        terms = segment(query.query)
        if query.query.strip() and not terms:
            return []
        conditions = ["p.status='active'",
                      "p.price_cents BETWEEN :low AND :high",
                      "(:category=0 OR p.category_id=:category)",
                      "p.attrs @> CAST(:attributes AS jsonb)",
                      "(NOT :available_only OR i.stock-i.reserved>0)"]
        params: dict[str, Any] = {'low': query.min_price_cent, 'high': query.max_price_cent,
                                  'category': query.category_id or 0,
                                  'attributes': json.dumps(query.attributes),
                                  'available_only': query.available_only}
        for index, term in enumerate(terms):
            conditions.append(f"(p.title_zh LIKE :t{index} OR p.description_zh LIKE :t{index})")
            params[f"t{index}"] = f"%{term}%"
        title_hits = " + ".join(f"(CASE WHEN p.title_zh LIKE :t{index} THEN 1 ELSE 0 END)"
                                for index in range(len(terms))) or "0"
        sql = f'''SELECT p.id,p.sku,p.title,p.title_zh,p.description_zh,p.category_id,p.price_cents,
            p.freight_cents,p.attrs,i.stock-i.reserved AS available,i.version AS inventory_version
          FROM commerce.products p JOIN commerce.inventory i ON p.id=i.product_id
          WHERE {" AND ".join(conditions)}
          ORDER BY ({title_hits}) DESC, similarity(p.title_zh, :raw) DESC,
            p.price_cents+p.freight_cents, p.id
          LIMIT :limit OFFSET :offset'''
        params.update({'raw': query.query, 'limit': query.limit, 'offset': query.offset})
        async with self.engine.connect() as c:
            rows = await c.execute(text(sql), params)
            return [dict(r) for r in rows.mappings()]
