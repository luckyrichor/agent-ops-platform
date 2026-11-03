#!/usr/bin/env python3
"""S1 索引开关：create / drop / stats，仅作用于 agent_ops_p10_s1。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
NAME = "s1_queue_partial"
DDL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS s1_queue_partial ON support.tickets"
    " (priority DESC, created_at, ticket_id) WHERE state='waiting_human'"
)


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
    args = parser.parse_args()
    started = time.perf_counter()
    with psycopg.connect(url(), autocommit=True) as connection, connection.cursor() as cursor:
        if args.action == "create":
            cursor.execute(DDL)
        elif args.action == "drop":
            cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS support.{NAME}")
        cursor.execute(
            "SELECT indexname, pg_relation_size(schemaname||'.'||indexname)"
            " FROM pg_indexes WHERE schemaname='support' AND indexname=%s", (NAME,)
        )
        row = cursor.fetchone()
        cursor.execute(
            "SELECT pg_size_pretty(pg_relation_size(schemaname||'.'||indexname))"
            " FROM pg_indexes WHERE schemaname='support' AND indexname=%s", (NAME,)
        )
        pretty = cursor.fetchone()
    print(json.dumps({
        "action": args.action,
        "exists": row is not None,
        "bytes": int(row[1]) if row else 0,
        "pretty": pretty[0] if pretty else None,
        "seconds": round(time.perf_counter() - started, 2),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
