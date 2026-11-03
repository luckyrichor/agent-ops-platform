#!/usr/bin/env python3
"""S1 数据准备：独立库、50 个座席账号、100 万工单（5% 待人工）。

只写 agent_ops_p10_s1，不碰其它库。工单正文不生成，只生成标题级字段。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ("order_status", "return_request", "human", "clarify")
REASONS = ("ORDER_QUESTION", "RETURN_WINDOW", "HUMAN_ESCALATION", "NEEDS_DETAILS")


def database_url(name: str) -> str:
    override = os.environ.get("S1_DATABASE_URL", "")
    if override:
        return override.replace("postgresql+psycopg://", "postgresql://")
    values = {}
    for line in (ROOT / ".local" / "application.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    base = values["AGENT_OPS_DATABASE_URL"].rsplit("/", 1)[0]
    return f"{base.replace('postgresql+psycopg://', 'postgresql://')}/{name}"


def seed_agents(connection: psycopg.Connection) -> int:
    phash = PasswordHasher().hash("bench-only-password")
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM auth.accounts WHERE role='agent'")
        existing = cursor.fetchone()[0]
        if existing >= 50:
            return existing
        for index in range(existing, 50):
            cursor.execute(
                "INSERT INTO auth.accounts (email,password_hash,role) VALUES (%s,%s,'agent')",
                (f"bench-agent-{index}@example.invalid", phash),
            )
    connection.commit()
    return 50


def seed_tickets(connection: psycopg.Connection, total: int, waiting_ratio: float,
                 seed: int) -> dict[str, int]:
    rng = random.Random(seed)
    waiting = int(total * waiting_ratio)
    states = ["waiting_human"] * waiting + ["resolved"] * (total - waiting)
    rng.shuffle(states)
    now = datetime.now(UTC)
    span = timedelta(days=90)

    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE support.ticket_events, support.messages, support.tickets CASCADE")
        with cursor.copy(
            "COPY support.tickets (ticket_id,route,state,revision,reason_code,order_status,"
            "history,created_at,updated_at,priority,customer_user_id) FROM STDIN"
        ) as copy:
            for index, state in enumerate(states):
                created = now - span * rng.random()
                route = ROUTES[index % len(ROUTES)]
                copy.write_row(
                    (
                        str(uuid.UUID(int=rng.getrandbits(128))),
                        route,
                        state,
                        1,
                        REASONS[index % len(REASONS)],
                        "delivered" if route == "order_status" else None,
                        '["opened"]',
                        created,
                        created,
                        rng.choices((0, 1, 2), weights=(1, 6, 3))[0],
                        None,
                    )
                )
    connection.commit()
    with connection.cursor() as cursor:
        cursor.execute("SELECT state,count(*) FROM support.tickets GROUP BY state ORDER BY 1")
        counts = dict(cursor.fetchall())
        cursor.execute("ANALYZE support.tickets")
    connection.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="agent_ops_p10_s1")
    parser.add_argument("--tickets", type=int, default=1_000_000)
    parser.add_argument("--waiting-ratio", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=ROOT / ".local" / "s1")
    args = parser.parse_args()

    url = database_url(args.database)
    with psycopg.connect(url, autocommit=False) as connection:
        agents = seed_agents(connection)
        started = datetime.now(UTC)
        counts = seed_tickets(connection, args.tickets, args.waiting_ratio, args.seed)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname, pg_relation_size(schemaname||'.'||indexname) "
                "FROM pg_indexes WHERE schemaname='support'"
            )
            indexes = cursor.fetchall()
            cursor.execute(
                "SELECT pg_size_pretty(pg_total_relation_size('support.tickets'))"
            )
            size = cursor.fetchone()[0]
    summary = {
        "database": args.database,
        "tickets": args.tickets,
        "waiting_ratio": args.waiting_ratio,
        "states": counts,
        "agents": agents,
        "seed_seconds": round(elapsed, 1),
        "table_size": size,
        "indexes": [{"name": name, "bytes": int(size)} for name, size in indexes],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "dataset.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
