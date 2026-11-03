#!/usr/bin/env python3
"""S1 索引开关：partial（仅待人工）与 composite（state+priority+created_at）。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"partial": "s1_queue_partial", "composite": "s1_queue_composite"}
DDL = {
    "partial": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS s1_queue_partial ON support.tickets"
        " (priority DESC, created_at, ticket_id) WHERE state='waiting_human'"
    ),
    "composite": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS s1_queue_composite ON support.tickets"
        " (state, priority DESC, created_at)"
    ),
}


def url() -> str:
    override = os.environ.get("S1_DATABASE_URL", "")
    if override:
        return override.replace("postgresql+psycopg://", "postgresql://")
    values = {}
    for line in (ROOT / ".local" / "application.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    base = values["AGENT_OPS_DATABASE_URL"].rsplit("/", 1)[0]
    return base.replace("postgresql+psycopg://", "postgresql://") + "/agent_ops_p10_s1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "drop", "stats"))
    parser.add_argument("--kind", choices=("partial", "composite"), default="partial")
    args = parser.parse_args()
    name = NAMES[args.kind]
    started = time.perf_counter()
    with psycopg.connect(url(), autocommit=True) as connection, connection.cursor() as cursor:
        if args.action == "create":
            cursor.execute(DDL[args.kind])
        elif args.action == "drop":
            cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS support.{name}")
        cursor.execute(
            "SELECT indexname, pg_relation_size(schemaname||'.'||indexname),"
            " pg_size_pretty(pg_relation_size(schemaname||'.'||indexname))"
            " FROM pg_indexes WHERE schemaname='support' AND indexname=%s", (name,)
        )
        row = cursor.fetchone()
        cursor.execute(
            "SELECT indexname, pg_size_pretty(pg_relation_size(schemaname||'.'||indexname))"
            " FROM pg_indexes WHERE schemaname='support'"
            " AND indexname IN ('s1_queue_partial','s1_queue_composite') ORDER BY 1"
        )
        present = cursor.fetchall()
    print(json.dumps({
        "action": args.action,
        "kind": args.kind,
        "exists": row is not None,
        "bytes": int(row[1]) if row else 0,
        "pretty": row[2] if row else None,
        "present": [{"name": item[0], "size": item[1]} for item in present],
        "seconds": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
