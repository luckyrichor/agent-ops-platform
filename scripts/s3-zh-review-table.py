#!/usr/bin/env python3
"""S3 阶段2：固定中文查询集的人工可读核对表（供 Codex 人工审阅）。

对 evals/s3/zh-queries.json 每条查询列出：query、预期类目（中文主词）、
实际 top5 的 title_zh/description_zh/类目主词/价格。语料为 zh_catalog
确定性合成商品（bench 库 20 万行），非真实商品；本表不声称做过人工标注，
只提供人工可读材料。输出：JSON（私有 .local/s3/）+ Markdown（私有+公开副本，
公开副本同为合成内容）。
"""
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.zh_catalog import CATEGORY_ZH

QUERIES_FILE = Path("evals/s3/zh-queries.json")


async def main() -> None:
    spec = json.loads(QUERIES_FILE.read_text())
    url = sys.argv[1] if len(sys.argv) > 1 else Path(".local/s3/bench.url").read_text().strip()
    search = CatalogSearch(url)
    entries = []
    for item in spec["queries"]:
        rows = await search.search(CatalogQuery(query=item["query"], limit=5))
        expected = "、".join(CATEGORY_ZH[c]["head"] for c in item["expected_categories"])
        top5 = [{"id": row["id"], "title_zh": row.get("title_zh") or "",
                 "description_zh": row.get("description_zh") or "",
                 "category": CATEGORY_ZH.get(row["category_id"], {}).get("head", str(row["category_id"])),
                 "category_id": row["category_id"],
                 "price_cents": row["price_cents"]} for row in rows]
        entries.append({"id": item["id"], "query": item["query"],
                        "qualifier": item.get("qualifier"), "expected_categories": expected,
                        "top5": top5})
    await search.aclose()
    meta = {"collected_at_beijing": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                                  time.gmtime(time.time() + 8 * 3600)),
            "queries_file_sha256": hashlib.sha256(QUERIES_FILE.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "note": ("语料为 zh_catalog 确定性合成商品（bench 库），表内全部为合成内容；"
                     "本表供人工审阅相关性，未声称已做人工标注"),
            "queries": entries}
    private = Path(".local/s3/zh-review-table.json")
    private.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    lines = ["# 中文检索人工核对表（合成语料，2026-11-04 北京时间生成）", "",
             f"脚本 SHA256：`{meta['script_sha256']}`；查询集 SHA256：`{meta['queries_file_sha256']}`",
             ("判定口径（供审阅者参考，非自动判定）：top 结果 title_zh 应含预期类目主词且类目一致；"
              "带 qualifier 的查询还须 description_zh 含该修饰词。"), ""]
    for entry in entries:
        lines.append(f"## {entry['id']}  查询：{entry['query']}"
                     + (f"（qualifier：{entry['qualifier']}）" if entry["qualifier"] else ""))
        lines.append(f"- 预期类目：{entry['expected_categories']}")
        if not entry["top5"]:
            lines.append("- 实际 top5：（无结果）")
        for rank, row in enumerate(entry["top5"], 1):
            lines.append(f"- {rank}. [{row['id']}] {row['title_zh']}｜{row['description_zh']}"
                         f"｜类目：{row['category']}｜价格：{row['price_cents'] / 100:.2f} 元")
        lines.append("")
    table_md = "\n".join(lines)
    Path(".local/s3/zh-review-table.md").write_text(table_md + "\n")
    public_dir = Path("docs/measurements/2026-11-03-s3")
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "zh-review-table-synthetic.md").write_text(table_md + "\n")
    (public_dir / "zh-review-table-synthetic.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
    print(f"ok: {private} + docs/measurements/2026-11-03-s3/zh-review-table-synthetic.md "
          f"({len(entries)} queries)")


if __name__ == "__main__":
    asyncio.run(main())
