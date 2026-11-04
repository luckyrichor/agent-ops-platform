#!/usr/bin/env python3
"""S3 短测量闸门（2026-11-04）：商品详情实体路径 / 中文检索 / 真实导购链路三段短基线。

边界（任务 2026-11-04-s3-measurement-gate.md）：
- 只做短测量，不运行完整 S3 矩阵、不实现 Redis、不调用真实业务模型。
- CommerceBackend.product 使用产品原方法（含库存 JOIN、连接池获取），不自造简化 SQL。
- 中文 CatalogSearch 使用产品原有路径（limit=10，与前置检索基线同口径）。
- Shopping.graph 为产品真实导购链路（deterministic Planner、本地角色、memory=None）；
  检索 span 经子类包装 CatalogSearch.search 计时（内部仍调用真实 database_query）。
- warmup 与计时窗口分离；分位数 nearest-rank（agent_ops.bench.stats.nearest_rank）。
- 连接池会话守卫：statement_timeout=5s、max_parallel_workers_per_gather=0（仅本进程连接）；
  共享 PG 未设本轮整机 CPU/内存配额，如实记录。
- 输出不含 DSN/凭据；脚本可复跑，原始逐请求延迟全部保留。
"""
import argparse
import asyncio
import contextvars
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import event, text

from agent_ops.bench.stats import nearest_rank
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.commerce_backend import CommerceBackend
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import DeterministicPlanner
from agent_ops.shopping import Shopping
from agent_ops.zh_catalog import CATEGORY_ZH

QUERIES_FILE = Path("evals/s3/zh-queries.json")
BENCH_URL_FILE = Path(".local/s3/bench.url")

# 固定确定性参数（无随机化）：product 基线
PRODUCT_REQUESTS_PER_ROUND = 300
PRODUCT_WARMUP = 30
# catalog 基线：30 查询 × 3 循环 = 90 请求/轮
CATALOG_CYCLES_PER_ROUND = 3
CATALOG_WARMUP_QUERIES = 60
CATALOG_LIMIT = 10
# 导购链路：同 30 查询 × 3 循环 = 90 请求/轮
SHOPPING_CYCLES_PER_ROUND = 3
SHOPPING_WARMUP_QUERIES = 30
SHOPPING_BUDGET_CENT = 10**9
SHOPPING_REGION = "上海"
ID_STRIDE = 667  # 与 200000 互素的固定步长，确定性均匀覆盖
CONCURRENCY = 2


def with_session_guard(dsn: str) -> str:
    """本测量连接池的会话级守卫，仅作用于本进程新建连接。"""
    options = "options=-c%20max_parallel_workers_per_gather%3D0%20-c%20statement_timeout%3D5000"
    return dsn + ("&" if "?" in dsn else "?") + options


def resolve_dsn(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    env = os.environ.get("S3_BENCH_DSN", "")
    if env:
        return env
    if BENCH_URL_FILE.exists():
        return BENCH_URL_FILE.read_text().strip()
    raise SystemExit("no DSN: pass --database-url or set S3_BENCH_DSN")


class DbTiming:
    """SQLAlchemy 事件计时：连接持有/SQL执行/窗口内计数；不是连接获取等待。"""

    def __init__(self, engine) -> None:
        self.hold_ms: list[float] = []
        self.sql_ms: list[float] = []
        self.sql_events = 0
        self.sql_failures = 0
        self.window_active = False
        sync_engine = engine.sync_engine

        def on_checkout(raw_connection, record, number):
            record.info["checkout_at"] = time.perf_counter()
            record.info["in_window"] = self.window_active

        def on_checkin(raw_connection, record):
            start = record.info.get("checkout_at")
            if start is not None and record.info.get("in_window"):
                self.hold_ms.append((time.perf_counter() - start) * 1000)

        def on_before(conn, cursor, statement, parameters, context, executemany):
            context._bench_t0 = time.perf_counter()
            if context.connection.info.get("in_window"):
                self.sql_events += 1

        def on_after(conn, cursor, statement, parameters, context, executemany):
            if context.connection.info.get("in_window"):
                self.sql_ms.append((time.perf_counter() - context._bench_t0) * 1000)

        def on_handle_error(context):
            connection = getattr(context, "connection", None)
            if connection is not None and connection.info.get("in_window"):
                self.sql_failures += 1

        event.listen(sync_engine, "checkout", on_checkout)
        event.listen(sync_engine, "checkin", on_checkin)
        event.listen(sync_engine, "before_cursor_execute", on_before)
        event.listen(sync_engine, "after_cursor_execute", on_after)
        event.listen(sync_engine, "handle_error", on_handle_error)

    def mark(self) -> None:
        """每轮开始时重置计数/采样，保证各轮 db 数据互不累计。"""
        self.hold_ms.clear()
        self.sql_ms.clear()
        self.sql_events = 0
        self.sql_failures = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "sql_events_in_window": self.sql_events,
            "sql_failures_in_window": self.sql_failures,
            "sql_exec_ms_raw": [round(v, 3) for v in self.sql_ms],
            "pool_hold_ms_raw": [round(v, 3) for v in self.hold_ms],
            "pool_hold_ms_nearest_rank": {
                "p50": round(nearest_rank(sorted(self.hold_ms), 50), 2),
                "p95": round(nearest_rank(sorted(self.hold_ms), 95), 2)} if self.hold_ms else "unavailable",
            "sql_exec_ms_nearest_rank": {
                "p50": round(nearest_rank(sorted(self.sql_ms), 50), 2),
                "p95": round(nearest_rank(sorted(self.sql_ms), 95), 2)} if self.sql_ms else "unavailable",
            "note": ("hold=checkout→checkin（含执行，非连接获取等待）；sql_exec=cursor 执行事件；"
                     "raw采样可重算任意分位数；原r2的pool_acquire字段为误命名，见报告勘误"),
        }


async def corpus_fingerprint(engine) -> dict[str, Any]:
    """语料/环境指纹：不含 DSN；逐行聚合哈希覆盖影响过滤/排序的字段。"""
    columns = ["id", "sku", "category_id", "title_zh", "description_zh", "price_cents",
               "freight_cents", "status", "attrs", "stock", "reserved", "inventory_version"]
    digest = hashlib.sha256()
    async with engine.connect() as conn:
        products = (await conn.execute(text("SELECT count(*) FROM commerce.products"))).scalar_one()
        pg_version = (await conn.execute(text("SHOW server_version_num"))).scalar_one()
        result = await conn.stream(text(
            "SELECT p.id, p.sku, p.category_id, coalesce(p.title_zh,''), coalesce(p.description_zh,''), "
            "p.price_cents, p.freight_cents, p.status, coalesce(p.attrs::text,''), "
            "i.stock, i.reserved, i.version "
            "FROM commerce.products p JOIN commerce.inventory i ON p.id=i.product_id ORDER BY p.id"))
        async for row in result:
            digest.update(("|".join(str(v) for v in row) + "\n").encode("utf-8"))
    root = Path(__file__).resolve().parents[1]
    source_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
        "src/agent_ops/commerce_backend.py", "src/agent_ops/catalog_search.py",
        "src/agent_ops/shopping.py", "src/agent_ops/orchestrator.py",
        "src/agent_ops/planner.py", "src/agent_ops/agents.py", "src/agent_ops/models.py",
        "src/agent_ops/bench/stats.py", "src/agent_ops/zh_catalog.py")}
    return {"products": products, "pg_server_version_num": pg_version,
            "corpus_content_sha256": digest.hexdigest(), "corpus_columns": columns,
            "queries_file_sha256": hashlib.sha256(QUERIES_FILE.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_file_sha256": source_hashes,
            "python_version": platform.python_version(),
            "collected_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def latency_stats(latencies: list[float]) -> dict[str, Any]:
    ordered = sorted(latencies)
    return {"p50": round(nearest_rank(ordered, 50), 3),
            "p95": round(nearest_rank(ordered, 95), 3),
            "p99": round(nearest_rank(ordered, 99), 3),
            "mean": round(sum(ordered) / len(ordered), 3),
            "max": round(ordered[-1], 3),
            "quantile_method": "nearest-rank (agent_ops.bench.stats.nearest_rank)"}


async def load_valid_ids(engine) -> list[int]:
    """真实存在且 active 的商品 ID（含库存 JOIN 条件下的有效主键）。"""
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT p.id FROM commerce.products p JOIN commerce.inventory i ON p.id=i.product_id "
            "WHERE p.status='active' AND i.stock-i.reserved>0 ORDER BY p.id"))
        return [int(r[0]) for r in rows]


def deterministic_ids(ids: list[int], count: int, offset: int) -> list[int]:
    """确定性均匀抽样：固定步长 667 与总量互素，offset 区分 warmup/各轮，不重叠。"""
    return [ids[(offset + i * ID_STRIDE) % len(ids)] for i in range(count)]


async def run_product_round(backend: CommerceBackend, timing: DbTiming, ids: list[int],
                            count: int, offset: int) -> dict[str, Any]:
    """商品详情实体路径：产品原方法 CommerceBackend.product(product_id)。

    warmup 独立于计时窗口；KeyError（不存在）也按原方法语义计为该 id 的失败。
    """
    chosen = deterministic_ids(ids, count, offset)
    warmup = deterministic_ids(ids, PRODUCT_WARMUP, offset + count * ID_STRIDE)
    timing.mark()
    for pid in warmup:
        try:
            await backend.product(pid)
        except KeyError:
            pass
    per_request: list[dict[str, Any]] = []
    timing.window_active = True
    started = time.perf_counter()
    window_start = time.time()

    async def worker(items: list[int]) -> None:
        for pid in items:
            begin = time.perf_counter()
            ok = True
            try:
                await backend.product(pid)
            except KeyError:
                ok = False
            except Exception:  # noqa: BLE001 - 错误单列不中断
                ok = False
            finally:
                per_request.append({"product_id": pid, "success": ok,
                                    "latency_ms": round((time.perf_counter() - begin) * 1000, 3)})

    chunks = [chosen[i::CONCURRENCY] for i in range(CONCURRENCY)]
    await asyncio.gather(*(worker(chunk) for chunk in chunks))
    elapsed = time.perf_counter() - started
    timing.window_active = False
    window_end = time.time()
    latencies = [r["latency_ms"] for r in per_request]
    return {"requests": len(per_request), "concurrency": CONCURRENCY,
            "warmup_requests": len(warmup),
            "id_selection": "确定性均匀：步长667（与N互素）取模，warmup偏移不与计时样本重叠",
            "window": {"start_unix": window_start, "end_unix": window_end, "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start)),
                       "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end)),
                       "duration_seconds": round(elapsed, 3)},
            "qps": round(len(per_request) / elapsed, 2),
            "latency_ms_nearest_rank": latency_stats(latencies),
            "per_request": per_request,
            "db": timing.snapshot(),
            "errors": {"count": sum(1 for r in per_request if not r["success"])}}


async def run_catalog_round(search: CatalogSearch, timing: DbTiming, queries: list[dict],
                            cycles: int, warmup_queries: int) -> dict[str, Any]:
    """中文检索原路径：CatalogSearch.search（limit=10，同前置检索基线口径）。"""
    for i in range(warmup_queries):
        await search.search(CatalogQuery(query=queries[i % len(queries)]["query"], limit=CATALOG_LIMIT))
    timing.mark()
    items = [queries[i % len(queries)] for i in range(len(queries) * cycles)]
    per_request: list[dict[str, Any]] = []
    timing.window_active = True
    started = time.perf_counter()
    window_start = time.time()

    async def worker(indices: list[int]) -> None:
        for index in indices:
            item = items[index]
            begin = time.perf_counter()
            ok, error = True, None
            try:
                await search.search(CatalogQuery(query=item["query"], limit=CATALOG_LIMIT))
            except Exception as exc:  # noqa: BLE001 - 错误单列不中断
                ok, error = False, type(exc).__name__
            finally:
                per_request.append({"index": index, "query_id": item["id"], "success": ok,
                                    "latency_ms": round((time.perf_counter() - begin) * 1000, 3),
                                    **({"error": error} if error else {})})

    chunks = [list(range(len(items)))[i::CONCURRENCY] for i in range(CONCURRENCY)]
    await asyncio.gather(*(worker(chunk) for chunk in chunks))
    elapsed = time.perf_counter() - started
    timing.window_active = False
    window_end = time.time()
    latencies = [r["latency_ms"] for r in per_request]
    return {"requests": len(per_request), "concurrency": CONCURRENCY,
            "warmup_requests": warmup_queries,
            "limit": CATALOG_LIMIT, "query_count": len(queries),
            "window": {"start_unix": window_start, "end_unix": window_end, "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start)),
                       "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end)),
                       "duration_seconds": round(elapsed, 3)},
            "qps": round(len(per_request) / elapsed, 2),
            "latency_ms_nearest_rank": latency_stats(latencies),
            "per_request": per_request,
            "db": timing.snapshot(),
            "errors": {"count": sum(1 for r in per_request if not r["success"])}}


class _SpanTimedCatalog(CatalogSearch):
    """子类包装 search() 计时（检索 span）；内部仍执行真实 database_query，非 mock。"""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.spans: dict[int, list[float]] = {}
        self._key = contextvars.ContextVar("shopping_request_key", default=None)

    def bind(self, key: int) -> object:
        return self._key.set(key)

    def unbind(self, token: object) -> None:
        self._key.reset(token)

    async def search(self, query: CatalogQuery) -> list[dict[str, Any]]:
        key = self._key.get()
        begin = time.perf_counter()
        try:
            return await super().search(query)
        finally:
            if key is not None:
                self.spans.setdefault(key, []).append((time.perf_counter() - begin) * 1000)


def shopping_body(item: dict) -> CommerceRequest:
    """产品真实导购请求体：中文查询 + 对应类目主词；预算/区域固定且不触发过滤收缩。"""
    category = CATEGORY_ZH[item["expected_categories"][0]]["head"]
    return CommerceRequest(request=item["query"], category=category,
                           budget_cent=SHOPPING_BUDGET_CENT, region=SHOPPING_REGION)


async def run_shopping_round(catalog: _SpanTimedCatalog, shopping: Shopping, timing: DbTiming,
                             queries: list[dict], cycles: int, warmup_queries: int,
                             round_index: int) -> dict[str, Any]:
    """真实导购链路：Shopping.graph（deterministic Planner、本地角色、memory=None）。

    每请求恰好一次 CatalogSearch.search（Shopping.catalog，limit=100）；
    检索 span=await search() 耗时（含连接获取/SQL/行映射）。
    """
    key_base = round_index * 1_000_000
    timing.mark()
    for i in range(warmup_queries):
        await shopping.graph(shopping_body(queries[i % len(queries)]))
    items = [queries[i % len(queries)] for i in range(len(queries) * cycles)]
    per_request: list[dict[str, Any]] = []
    timing.window_active = True
    started = time.perf_counter()
    window_start = time.time()

    async def worker(indices: list[int]) -> None:
        for index in indices:
            item = items[index]
            key = key_base + index
            token = catalog.bind(key)
            begin = time.perf_counter()
            ok, error = True, None
            try:
                result = await shopping.graph(shopping_body(item))
                if result.outcome != "succeeded":
                    ok, error = False, "outcome:" + result.outcome
            except Exception as exc:  # noqa: BLE001 - 错误单列不中断
                ok, error = False, type(exc).__name__
            finally:
                e2e = (time.perf_counter() - begin) * 1000
                catalog.unbind(token)
                span = catalog.spans.get(key, [])
                span_ms = span[0] if span else None
                ratio = round(span_ms / e2e, 4) if span_ms else None
                per_request.append({"index": index, "query_id": item["id"], "success": ok,
                                    "e2e_ms": round(e2e, 3),
                                    "retrieval_span_ms": None if span_ms is None else round(span_ms, 3),
                                    "retrieval_span_over_e2e": ratio,
                                    **({"error": error} if error else {})})

    chunks = [list(range(len(items)))[i::CONCURRENCY] for i in range(CONCURRENCY)]
    await asyncio.gather(*(worker(chunk) for chunk in chunks))
    elapsed = time.perf_counter() - started
    timing.window_active = False
    window_end = time.time()
    e2e = [r["e2e_ms"] for r in per_request]
    spans = [r["retrieval_span_ms"] for r in per_request if r["retrieval_span_ms"] is not None]
    ratios = [r["retrieval_span_over_e2e"] for r in per_request
              if r["retrieval_span_over_e2e"] is not None]
    return {"requests": len(per_request), "concurrency": CONCURRENCY,
            "warmup_requests": warmup_queries,
            "planner": "deterministic", "memory": None,
            "catalog_limit_in_shopping": 100,
            "fixed_request_fields": {"budget_cent": SHOPPING_BUDGET_CENT, "region": SHOPPING_REGION,
                                      "category": "查询 expected_categories 首类目主词"},
            "window": {"start_unix": window_start, "end_unix": window_end, "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start)),
                       "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end)),
                       "duration_seconds": round(elapsed, 3)},
            "qps": round(len(per_request) / elapsed, 2),
            "e2e_ms_nearest_rank": latency_stats(e2e),
            "retrieval_span_ms_nearest_rank": latency_stats(spans),
            "retrieval_span_over_e2e": {
                "per_request_mean": round(sum(ratios) / len(ratios), 4),
                "per_request_min": round(min(ratios), 4),
                "per_request_max": round(max(ratios), 4),
                "definition": "逐请求 span_ms/e2e_ms 的比值；均值/极值均为逐请求比值的统计，"
                              "不是分位数相除"},
            "per_request": per_request,
            "db": timing.snapshot(),
            "errors": {"count": sum(1 for r in per_request if not r["success"])}}


PROVENANCE = {
    "percentiles": "全部 p50/p95/p99 = nearest_rank(sorted(latencies), q)；原始逐请求延迟在 per_request",
    "qps": "窗口请求数 / perf_counter 窗口时长",
    "shopping_ratio": "retrieval_span_over_e2e 为逐请求 span/e2e 的比值；均值是对逐请求比值求算术平均",
    "db_timings": "SQLAlchemy checkout/checkin为持有时间，before/after_cursor_execute为SQL执行事件，非纯连接等待",
    "id_selection": "product：真实存在且 active 且可售的 id 集合，固定步长 667 取模确定性均匀选择",
    "warmup": "warmup 请求在计时窗口外执行，不进入任何指标",
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--phases", default="product,catalog,shopping",
                        help="逗号分隔：product,catalog,shopping；本轮未触发L1分支，不提供未验证对照")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.rounds < 3:
        raise SystemExit("--rounds must be >= 3 (短轮观察抖动)")
    phases = args.phases.split(",")
    if not phases or set(phases) - {"product", "catalog", "shopping"} or len(phases) != len(set(phases)):
        raise SystemExit("invalid or duplicate phases")
    if Path(args.out).exists():
        raise SystemExit("output exists; choose a new file")
    dsn = with_session_guard(resolve_dsn(args.database_url))
    spec = json.loads(QUERIES_FILE.read_text())
    queries = spec["queries"]

    backend = CommerceBackend(dsn)
    search = CatalogSearch(dsn)
    catalog = None
    dispatcher = None
    report: dict[str, Any] = {"status": "failed_before_fingerprint", "phases": {}}
    try:
        report: dict[str, Any] = {
            "task": "2026-11-04-s3-measurement-gate",
            "fingerprint": await corpus_fingerprint(search.engine),
            "pg_session_guard": "options=-c max_parallel_workers_per_gather=0 -c statement_timeout=5000 "
                                "（仅本测量连接池；共享 PG 未设本轮整机限额）",
            "engine_note": "CommerceBackend 引擎 pool_size=5/max_overflow=5/pool_pre_ping；"
                           "CatalogSearch 引擎 pool_pre_ping（默认池）",
            "provenance": PROVENANCE,
            "rounds": args.rounds,
            "phases": {},
        }

        ids: list[int] = []
        if "product" in phases:
            ids = await load_valid_ids(search.engine)

        if "product" in phases:
            timing = DbTiming(backend.engine)
            rounds = []
            for r in range(args.rounds):
                rounds.append(await run_product_round(backend, timing, ids,
                                                      PRODUCT_REQUESTS_PER_ROUND,
                                                      r * (PRODUCT_REQUESTS_PER_ROUND + PRODUCT_WARMUP)))
            report["phases"]["product"] = {
                "method": "CommerceBackend.product(product_id) 产品原方法（含库存JOIN/连接池获取）",
                "rounds": rounds}

        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")

        if "catalog" in phases:
            timing = DbTiming(search.engine)
            rounds = []
            for _ in range(args.rounds):
                rounds.append(await run_catalog_round(search, timing, queries,
                                                      CATALOG_CYCLES_PER_ROUND,
                                                      CATALOG_WARMUP_QUERIES))
            report["phases"]["catalog"] = {
                "method": "CatalogSearch.search 中文原路径（同前置检索基线口径）",
                "rounds": rounds}

        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")

        if "shopping" in phases:
            catalog = _SpanTimedCatalog(dsn)
            dispatcher = Dispatcher({})
            shopping = Shopping(catalog, dispatcher, DeterministicPlanner(), None)
            timing = DbTiming(catalog.engine)
            rounds = []
            for r in range(args.rounds):
                rounds.append(await run_shopping_round(catalog, shopping, timing, queries,
                                                       SHOPPING_CYCLES_PER_ROUND,
                                                       SHOPPING_WARMUP_QUERIES, r))
            report["phases"]["shopping"] = {
                "method": "Shopping.graph 真实导购链路（deterministic Planner/本地角色/memory=None）；"
                          "检索 span 经子类包装 search() 计时，内部仍真实查库",
                "rounds": rounds}

        report["status"] = "completed"
        text_out = json.dumps(report, ensure_ascii=False, indent=1)
        Path(args.out).write_text(text_out + "\n")
        summary = {phase: [
            {"requests": rd["requests"], "qps": rd["qps"],
             "latency": rd.get("latency_ms_nearest_rank") or rd.get("e2e_ms_nearest_rank"),
             "errors": rd["errors"]["count"]} for rd in data["rounds"]]
            for phase, data in report["phases"].items()}
        print(json.dumps({"fingerprint_products": report["fingerprint"]["products"],
                          "summary": summary}, ensure_ascii=False))
    except BaseException as error:  # noqa: BLE001 - checkpoint failure without logging DSN
        report["status"] = "failed"
        report["failure_type"] = type(error).__name__
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        raise SystemExit("measurement failed: " + type(error).__name__) from None
    finally:
        if catalog is not None:
            await catalog.aclose()
        if dispatcher is not None:
            await dispatcher.aclose()
        await backend.aclose()
        await search.aclose()


if __name__ == "__main__":
    asyncio.run(main())
