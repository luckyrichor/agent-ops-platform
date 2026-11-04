#!/usr/bin/env python3
"""S3 前置 b：无业务模型的中文检索基准（可复跑，固定快照/查询集）。

只测产品检索路径（CatalogSearch 中文分支），不调用 planner/模型。
口径（2026-11-03 按 Codex 评审修正）：
- precision@10 = |top10 ∩ 标签| / 10；recall@10 = |top10 ∩ 标签| / |全部标签|，
  分母 0 的查询单列 undefined，不并入均值。标签独立于实现的排序/分词：
  由查询集结构化字段（expected_categories + 类目主词 + qualifier）确定性生成，
  并应用与检索相同的 available/价格默认过滤；属合成语料标签，非人工标注。
- 连接获取耗时 = await engine.connect() 前后计时；SQL 执行耗时经
  before/after_cursor_execute 事件计时；checkout→checkin 仅报告为连接持有
  （hold），不混称获取等待。
- DB 查询次数 = 计时窗口内提交给 DB 的 SQL 执行事件（含失败单列）；
  窗口外查询（指纹/标签构建）单独计数。
- 预热与正式窗口分离；查询顺序固定（无随机化）；错误响应单列并记录样本。
- 分位数统一 nearest-rank（agent_ops.bench.stats.nearest_rank）。
- 指纹：脚本/查询集/语料内容完整 SHA256；窗口起止 UTC 时间与样本数可重算。
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import event, text

from agent_ops.bench.stats import nearest_rank
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.zh_catalog import CATEGORY_ZH

QUERIES_FILE = Path("evals/s3/zh-queries.json")
BENCH_URL_FILE = Path(".local/s3/bench.url")


def with_session_guard(dsn: str) -> str:
    """本测量连接池的会话级守卫：max_parallel_workers_per_gather=0、statement_timeout=5s。

    通过 psycopg conninfo options 传入，仅作用于本进程引擎新建连接，
    不更改共享 role/全局配置。
    """
    options = "options=-c%20max_parallel_workers_per_gather%3D0%20-c%20statement_timeout%3D5000"
    return dsn + ("&" if "?" in dsn else "?") + options


def resolve_dsn(arg_value: str | None) -> str:
    """先解析参数再取默认：env S3_BENCH_DSN，最后才是私有文件（--help 不读文件）。"""
    if arg_value:
        return arg_value
    env = os.environ.get("S3_BENCH_DSN", "")
    if env:
        return env
    if BENCH_URL_FILE.exists():
        return BENCH_URL_FILE.read_text().strip()
    raise SystemExit("no DSN: pass --database-url or set S3_BENCH_DSN")


class _TimedConnect:
    """包装 AsyncConnection 上下文：acquire 计时 + 窗口标记（acquire=connect 完成时刻）。"""

    def __init__(self, cm, record: dict) -> None:
        self._cm = cm
        self._record = record
        self._t0 = time.perf_counter()

    async def __aenter__(self):
        conn = await self._cm.__aenter__()
        active = self._record["active"]
        conn.sync_connection.info["in_window"] = active
        if active:
            self._record["acquire_ms"].append((time.perf_counter() - self._t0) * 1000)
        return conn

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class _TimedEngine:
    """委托 AsyncEngine，仅 connect() 增加获取计时；CatalogSearch 无需改动。"""

    def __init__(self, engine, record: dict) -> None:
        self._engine = engine
        self._record = record

    def connect(self):
        return _TimedConnect(self._engine.connect(), self._record)

    def __getattr__(self, name):
        return getattr(self._engine, name)


async def corpus_sha256(search: CatalogSearch) -> tuple[str, list[str]]:
    """语料内容完整指纹：逐行聚合哈希，覆盖全部影响过滤/排序的字段。

    字段含 id/title_zh/description_zh/类目/价格/运费/status 与库存侧
    available 输入 stock/reserved、并发控制 version、属性 attrs。
    """
    columns = ["id", "sku", "category_id", "title_zh", "description_zh", "price_cents",
               "freight_cents", "status", "attrs", "stock", "reserved", "inventory_version"]
    digest = hashlib.sha256()
    async with search.engine.connect() as conn:
        result = await conn.stream(text(
            "SELECT p.id, p.sku, p.category_id, coalesce(p.title_zh,''), coalesce(p.description_zh,''), "
            "p.price_cents, p.freight_cents, p.status, coalesce(p.attrs::text,''), "
            "i.stock, i.reserved, i.version "
            "FROM commerce.products p JOIN commerce.inventory i ON p.id=i.product_id ORDER BY p.id"))
        async for row in result:
            digest.update(("|".join(str(v) for v in row) + "\n").encode("utf-8"))
    return digest.hexdigest(), columns


async def label_ids(search: CatalogSearch, item: dict) -> set[int]:
    """确定性标签（不依赖实现排序/分词）：expected_categories 的类目主词 + qualifier，
    应用与检索一致的 active/available/默认价格过滤。"""
    heads = sorted({CATEGORY_ZH[c]["head"] for c in item["expected_categories"]})
    conditions = ["p.status='active'",
                  "p.category_id = ANY(:cats)",
                  "i.stock - i.reserved > 0"]
    params: dict = {"cats": list(item["expected_categories"])}
    term_conds = []
    for index, head in enumerate(heads):
        term_conds.append(f"p.title_zh LIKE :h{index}")
        params[f"h{index}"] = f"%{head}%"
    conditions.append("(" + " OR ".join(term_conds) + ")")
    if "qualifier" in item:
        conditions.append("p.description_zh LIKE :qual")
        params["qual"] = f"%{item['qualifier']}%"
    sql = ("SELECT p.id FROM commerce.products p JOIN commerce.inventory i "
           "ON p.id=i.product_id WHERE " + " AND ".join(conditions))
    async with search.engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).scalars()
        return set(rows)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=300, help="冒烟默认 300；正式矩阵由阶段2决定")
    parser.add_argument("--labels", action="store_true", help="计算 precision@10 与 recall@10")
    parser.add_argument("--warmup-per-worker", type=int, default=1,
                        help="每 worker 预热请求数（正式基线要求 ≥30）；预热在计时窗口外")
    parser.add_argument("--seed", type=int, default=0, help="保留字段：当前查询顺序固定为查询集原序循环")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if args.requests <= 0 or args.concurrency <= 0:
        raise SystemExit("--requests and --concurrency must be positive")
    if args.warmup_per_worker <= 0:
        raise SystemExit("--warmup-per-worker must be positive")
    dsn = with_session_guard(resolve_dsn(args.database_url))

    spec = json.loads(QUERIES_FILE.read_text())
    queries = spec["queries"]
    search = CatalogSearch(dsn)
    sql_stats = {"in_window": 0, "failed_in_window": 0, "outside_window": 0}
    acquire_ms: list[float] = []
    sql_ms: list[float] = []
    hold_ms: list[float] = []

    sync_engine = search.engine.sync_engine

    def on_checkout(raw_connection, record, number):
        record.info["checkout_at"] = time.perf_counter()

    def on_checkin(raw_connection, record):
        start = record.info.get("checkout_at")
        if start is not None and record.info.get("in_window"):
            hold_ms.append((time.perf_counter() - start) * 1000)

    def on_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        context._bench_t0 = time.perf_counter()
        if context.connection.info.get("in_window"):
            sql_stats["in_window"] += 1
        else:
            sql_stats["outside_window"] += 1

    def on_after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if context.connection.info.get("in_window"):
            sql_ms.append((time.perf_counter() - context._bench_t0) * 1000)

    def on_handle_error(context):
        connection = getattr(context, "connection", None)
        if connection is not None and connection.info.get("in_window"):
            sql_stats["failed_in_window"] += 1

    event.listen(sync_engine, "checkout", on_checkout)
    event.listen(sync_engine, "checkin", on_checkin)
    event.listen(sync_engine, "before_cursor_execute", on_before_cursor_execute)
    event.listen(sync_engine, "after_cursor_execute", on_after_cursor_execute)
    event.listen(sync_engine, "handle_error", on_handle_error)

    window = {"active": False, "acquire_ms": acquire_ms}
    search.engine = _TimedEngine(search.engine, window)

    fingerprint = {"products": 0, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "queries_file_sha256": hashlib.sha256(QUERIES_FILE.read_bytes()).hexdigest(),
                   "queries_version": spec["version"],
                   "source_file_sha256": {
                       "src/agent_ops/catalog_search.py": hashlib.sha256(
                           (Path(__file__).resolve().parents[1] / "src/agent_ops/catalog_search.py").read_bytes()).hexdigest(),
                       "src/agent_ops/zh_catalog.py": hashlib.sha256(
                           (Path(__file__).resolve().parents[1] / "src/agent_ops/zh_catalog.py").read_bytes()).hexdigest(),
                       "src/agent_ops/bench/stats.py": hashlib.sha256(
                           (Path(__file__).resolve().parents[1] / "src/agent_ops/bench/stats.py").read_bytes()).hexdigest()},
                   "label_rule":
                       "expected_categories 主词命中 title_zh（AND 类目、active、available、默认价格带；qualifier 命中 description_zh）；独立于实现排序/分词；合成标签，非人工标注"}
    async with search.engine.connect() as conn:
        conn.sync_connection.info["in_window"] = False
        fingerprint["products"] = (await conn.execute(
            text("SELECT count(*) FROM commerce.products"))).scalar_one()
    fingerprint["corpus_content_sha256"], fingerprint["corpus_columns"] = await corpus_sha256(search)

    # 预热：每 worker 一条查询，不计入窗口指标
    async def one_call(client_item: dict) -> tuple[bool, float]:
        begin = time.perf_counter()
        try:
            await search.search(CatalogQuery(query=client_item["query"], limit=10))
            return True, (time.perf_counter() - begin) * 1000
        except Exception:  # noqa: BLE001
            return False, (time.perf_counter() - begin) * 1000

    warmup_items = [queries[i % len(queries)] for i in range(args.concurrency * args.warmup_per_worker)]
    for item in warmup_items:
        await one_call(item)

    latencies: list[float] = []
    per_request: list[dict] = []
    errors: list[str] = []
    error_count = 0
    lock = asyncio.Lock()
    cycle = [queries[i % len(queries)] for i in range(args.requests)]
    window_start = time.time()
    window["active"] = True
    started = time.perf_counter()

    async def worker(indices: list[int]) -> None:
        nonlocal error_count
        for index in indices:
            item = cycle[index]
            begin = time.perf_counter()
            ok = True
            try:
                await search.search(CatalogQuery(query=item["query"], limit=10))
            except Exception as error:  # noqa: BLE001 - 错误单列不中断
                ok = False
                async with lock:
                    error_count += 1
                    if len(errors) < 5:
                        errors.append(f"{item['id']}: {type(error).__name__}")
            finally:
                latency = (time.perf_counter() - begin) * 1000
                async with lock:
                    latencies.append(latency)
                    per_request.append({"index": index, "query_id": item["id"],
                                        "success": ok, "latency_ms": round(latency, 3)})

    chunks = [list(range(start, args.requests, args.concurrency)) for start in range(args.concurrency)]
    await asyncio.gather(*(worker(chunk) for chunk in chunks))
    elapsed = time.perf_counter() - started
    window_end = time.time()
    window["active"] = False

    ordered = sorted(latencies)
    report: dict = {
        "fingerprint": fingerprint,
        "requests": args.requests, "concurrency": args.concurrency, "seed": args.seed,
        "warmup_requests": len(warmup_items),
        "pg_session_guard": "options=-c max_parallel_workers_per_gather=0 -c statement_timeout=5000 "
                            "(仅本测量连接池；PG 侧未单独限 CPU)",
        "window": {"start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start)),
                   "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end)),
                   "duration_seconds": round(elapsed, 3), "samples": len(latencies)},
        "qps": round(len(latencies) / elapsed, 2),
        "per_request": per_request,
        "latency_ms_nearest_rank": {
            "p50": round(nearest_rank(ordered, 50), 1),
            "p95": round(nearest_rank(ordered, 95), 1),
            "p99": round(nearest_rank(ordered, 99), 1)},
        "errors": {"count": error_count, "samples": errors},
        "db": {"sql_events_in_window": sql_stats["in_window"],
               "sql_failures_in_window": sql_stats["failed_in_window"],
               "sql_events_outside_window_at_window_end": sql_stats["outside_window"],
               "pool_acquire_ms_raw": [round(v, 3) for v in acquire_ms],
               "sql_exec_ms_raw": [round(v, 3) for v in sql_ms],
               "pool_acquire_ms_nearest_rank": {
                   "p50": round(nearest_rank(sorted(acquire_ms), 50), 2),
                   "p95": round(nearest_rank(sorted(acquire_ms), 95), 2)} if acquire_ms else "unavailable",
               "sql_exec_ms_nearest_rank": {
                   "p50": round(nearest_rank(sorted(sql_ms), 50), 2),
                   "p95": round(nearest_rank(sorted(sql_ms), 95), 2)} if sql_ms else "unavailable",
               "pool_hold_ms_total": round(sum(hold_ms), 1),
               "note": ("acquire=await engine.connect() 等待；sql_exec=cursor 执行事件计时；"
                        "hold=checkout→checkin 连接占用（含执行与上层处理，并发放大，非获取等待）；"
                        "raw 采样可重算任意分位数；outside_window 最终值在标签构建完成后回填 "
                        "db.sql_events_outside_window_final")},
    }

    if args.labels:
        top10_sets: dict[str, set[int]] = {}
        labels: dict[str, set[int]] = {}
        # 标签构建在窗口外执行，SQL 事件计入 outside_window
        for item in queries:
            rows = await search.search(CatalogQuery(query=item["query"], limit=10))
            top10_sets[item["id"]] = {row["id"] for row in rows}
            labels[item["id"]] = await label_ids(search, item)
        precisions: list[float] = []
        recalls: list[float] = []
        undefined: list[str] = []
        per_query: list[dict] = []
        for item in queries:
            label = labels[item["id"]]
            top = top10_sets[item["id"]]
            hits = len(top & label)
            precision = hits / 10
            precisions.append(precision)
            if label:
                recall = hits / len(label)
                recalls.append(recall)
            else:
                recall = None
                undefined.append(item["id"])
            per_query.append({"id": item["id"], "label_size": len(label),
                              "precision@10": round(precision, 4),
                              "recall@10": None if recall is None else round(recall, 4)})
        report["quality_at_10"] = {
            "precision_at_10_mean": round(sum(precisions) / len(precisions), 4),
            "recall_at_10_mean": round(sum(recalls) / len(recalls), 4) if recalls else None,
            "recall_denominator": "|label|（全部相关且符合过滤的候选数）；分母0单列，不并入均值",
            "queries_with_zero_labels": undefined,
            "per_query": per_query,
        }
    # 标签/指纹等窗口外工作全部结束后，回填 outside_window 最终计数
    report["db"]["sql_events_outside_window_final"] = sql_stats["outside_window"]
    text_out = json.dumps(report, ensure_ascii=False, indent=1)
    print(text_out)
    if args.out:
        Path(args.out).write_text(text_out + "\n")
    await search.aclose()


if __name__ == "__main__":
    asyncio.run(main())
