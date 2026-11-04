#!/usr/bin/env python3
"""S3 前置 a：30 条固定中文查询的机器初筛（bench 库合成语料，产品路径）。

走产品 CatalogSearch 中文分支（非 bench 另造检索器）。判定口径：
top5 每条 title_zh 含类目主词（evals/s3/zh-queries.json expected_categories
对应的 CATEGORY_ZH 主词，独立于实现分词）AND category_id ∈ expected_categories
（AND description_zh 含 qualifier，如适用）。只作机器初筛，非人工标注。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.zh_catalog import CATEGORY_ZH


def judge(item: dict, row: dict) -> dict[str, object]:
    head = CATEGORY_ZH[item["expected_categories"][0]]["head"]
    result: dict[str, object] = {"id": row["id"], "category_id": row["category_id"],
                                 "title_ok": head in (row.get("title_zh") or ""),
                                 "category_ok": row["category_id"] in item["expected_categories"]}
    if "qualifier" in item:
        result["qualifier_ok"] = item["qualifier"] in (row.get("description_zh") or "")
    return result


def row_ok(judged: dict[str, object]) -> bool:
    return bool(judged["title_ok"]) and bool(judged["category_ok"]) and judged.get("qualifier_ok", True) is True


async def main() -> None:
    spec = json.loads(Path("evals/s3/zh-queries.json").read_text())
    url = sys.argv[1] if len(sys.argv) > 1 else Path(".local/s3/bench.url").read_text().strip()
    search = CatalogSearch(url)
    results = []
    passed = 0
    for item in spec["queries"]:
        rows = await search.search(CatalogQuery(query=item["query"], limit=5))
        judged = [judge(item, row) for row in rows]
        ok = len(rows) == 5 and all(row_ok(row) for row in judged)
        passed += ok
        results.append({"id": item["id"], "query": item["query"], "top5_ok": ok, "rows": judged})
    print(json.dumps({"passed": passed, "total": len(results),
                      "note": "机器初筛：合成语料结构化判定，非人工相关性验收",
                      "results": results}, ensure_ascii=False, indent=1))
    await search.aclose()


if __name__ == "__main__":
    asyncio.run(main())
