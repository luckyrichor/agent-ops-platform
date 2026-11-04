#!/usr/bin/env python3
"""S3 前置 a：为 bench 库 20 万商品确定性生成类目一致的中文标题/描述。

只 UPDATE 合成的 title_zh / description_zh（迁移 0011 已建列与 trgm 索引），
不触碰真实用户/订单数据；同 id 同输出（zh_catalog 纯函数），可重复执行。
用法：
  uv run python scripts/s3-chinese-catalog.py --database-url <async url> [--batch 2000] [--limit N]
"""
import argparse
import asyncio
import json
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_ops.zh_catalog import CATEGORY_ZH, description_zh, title_zh


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个商品（冒烟用）")
    args = parser.parse_args()

    engine = create_async_engine(args.database_url)
    started = time.perf_counter()
    updated = 0
    batches = 0
    next_id = 0
    while True:
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT id, category_id FROM commerce.products WHERE id > :after "
                "ORDER BY id LIMIT :batch"), {"after": next_id, "batch": args.batch})).all()
            if not rows:
                break
            updates = []
            for product_id, category_id in rows:
                if category_id not in CATEGORY_ZH:
                    raise SystemExit(f"product {product_id} has unmapped category {category_id}")
                updates.append({"i": product_id, "t": title_zh(product_id, category_id),
                                "d": description_zh(product_id, category_id)})
            await conn.execute(text(
                "UPDATE commerce.products SET title_zh=:t, description_zh=:d WHERE id=:i"),
                updates)
        updated += len(rows)
        batches += 1
        next_id = rows[-1][0]
        if args.limit and updated >= args.limit:
            break
    print(json.dumps({"updated": updated, "batches": batches,
                      "elapsed_seconds": round(time.perf_counter() - started, 3),
                      "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
