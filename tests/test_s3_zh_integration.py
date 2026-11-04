"""S3 返修集成测试：产品 CatalogSearch 中文路径（短词/无结果/过滤语义）。

仅在提供 DSN（--database-url 参数env S3_BENCH_DSN 或 .local/s3/bench.url，
须为已应用 0011 且填充中文语料的合成 bench 库）时运行；无 DSN 跳过。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_ops.catalog_search import CatalogQuery, CatalogSearch

QUERIES = json.loads(Path("evals/s3/zh-queries.json").read_text())


def _dsn() -> str | None:
    if os.environ.get("S3_BENCH_DSN"):
        return os.environ["S3_BENCH_DSN"]
    if Path(".local/s3/bench.url").exists():
        return Path(".local/s3/bench.url").read_text().strip()
    return None


def _no_dsn() -> str:
    dsn = _dsn()
    if dsn is None:
        pytest.skip("no bench DSN for Chinese integration tests")
    return dsn


def test_migration_guard_fails_loudly_on_missing_columns() -> None:
    """未应用 0011 的库：中文查询必须显式报错，不得静默回退或空结果。"""
    pytest.importorskip("aiosqlite")
    # 用一个必然存在但无中文列的库：sqlite 不适用（asyncpg 方言），改为验证守卫逻辑本身
    from agent_ops.catalog_search import CatalogSearch
    search = CatalogSearch("sqlite+aiosqlite://")
    try:
        with pytest.raises(RuntimeError, match="0011"):
            asyncio.run(search.search(CatalogQuery(query="保温杯", limit=1)))
    finally:
        asyncio.run(search.aclose())


def test_short_word_and_exact_category() -> None:
    dsn = _no_dsn()
    search = CatalogSearch(dsn)
    try:
        rows = asyncio.run(search.search(CatalogQuery(query="猫粮", limit=10)))
        assert rows, "2 字短词应有结果"
        assert all(r["category_id"] == 64 for r in rows)
        assert all("猫粮" in (r.get("title_zh") or "") for r in rows)
    finally:
        asyncio.run(search.aclose())


def test_no_result_for_unknown_term() -> None:
    dsn = _no_dsn()
    search = CatalogSearch(dsn)
    try:
        rows = asyncio.run(search.search(CatalogQuery(query="不存在的词组xyz", limit=10)))
        assert rows == []
    finally:
        asyncio.run(search.aclose())


def test_filters_preserved_on_chinese_path() -> None:
    dsn = _no_dsn()
    search = CatalogSearch(dsn)
    try:
        unfiltered = asyncio.run(search.search(CatalogQuery(query="蓝牙耳机", limit=50)))
        assert unfiltered, "基准应有结果"
        # available_only=False 放宽后可用数 >= 默认
        relaxed = asyncio.run(search.search(
            CatalogQuery(query="蓝牙耳机", limit=50, available_only=False)))
        assert len(relaxed) >= len(unfiltered)
        # 价格过滤：全部行价格在界内
        bounded = asyncio.run(search.search(
            CatalogQuery(query="蓝牙耳机", limit=50, min_price_cent=10**9)))
        assert bounded == []
        # 类目过滤：指定其他类目 → 空或全部为该类目
        scoped = asyncio.run(search.search(
            CatalogQuery(query="蓝牙耳机", limit=50, category_id=1)))
        assert all(r["category_id"] == 1 for r in scoped)
        # 属性过滤：空属性匹配全部
        attrs = asyncio.run(search.search(
            CatalogQuery(query="蓝牙耳机", limit=50, attributes={"nope": "x"})))
        assert attrs == []
        # 返回字段含库存与 inventory_version（英文路径字段语义保持）
        assert "available" in unfiltered[0] and "inventory_version" in unfiltered[0]
    finally:
        asyncio.run(search.aclose())


def test_english_path_unchanged() -> None:
    dsn = _no_dsn()
    search = CatalogSearch(dsn)
    try:
        rows = asyncio.run(search.search(CatalogQuery(query="nonexistent-english-term-zz", limit=5)))
        assert rows == []
    finally:
        asyncio.run(search.aclose())
