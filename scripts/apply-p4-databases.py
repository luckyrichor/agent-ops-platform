"""P4 additive migration on existing P0/P2; private backup first, no clients interrupted."""

import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

folder = Path(".local/p4-2026-10-27")
folder.mkdir(mode=0o700, exist_ok=True)
report_path = folder / "databases.json"
if report_path.exists():
    raise SystemExit("maintenance report exists; inspect before any repeat")
secrets = dict(
    l.split("=", 1) for l in Path(".local/stack.env").read_text().splitlines() if "=" in l
)
app = dict(
    l.split("=", 1) for l in Path(".local/application.env").read_text().splitlines() if "=" in l
)
owner = make_url(app["AGENT_OPS_DATABASE_URL"]).set(
    username="ops_owner", password=secrets["POSTGRES_PASSWORD"]
)
report = {
    "hostname": socket.gethostname(),
    "started_at": datetime.now(UTC).isoformat(),
    "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "dirty": True,
    "databases": {},
}


def save():
    report_path.write_text(json.dumps(report, indent=2) + "\n")


def snapshot(c):
    result = {}
    for schema, table in [
        ("commerce", "users"),
        ("commerce", "orders"),
        ("support", "tickets"),
        ("support", "ticket_events"),
    ]:
        result[schema + "." + table] = c.execute(
            text(f"SELECT count(*) FROM {schema}.{table}")
        ).scalar_one()
    result["seed_sender_counts"] = dict(
        c.execute(text("SELECT sender,count(*) FROM commerce.messages GROUP BY sender")).all()
    )
    return result


with psycopg.connect(
    owner.set(database="postgres", drivername="postgresql").render_as_string(hide_password=False),
    autocommit=True,
) as admin:
    for database in ["agent_ops", "agent_ops_p2_seed42"]:
        r = report["databases"][database] = {}
        clients = admin.execute(
            "SELECT pid,state,application_name FROM pg_stat_activity WHERE datname=%s", (database,)
        ).fetchall()
        if clients:
            raise SystemExit("database busy; no clients terminated: " + database)
        r["preflight_sessions"] = clients
        engine = create_engine(owner.set(database=database).render_as_string(hide_password=False))
        with engine.connect() as c:
            assert (
                c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0004_auth"
            )
            r["before"] = snapshot(c)
            c.rollback()
            backup = Path(".local/backups") / (
                database + "-pre-0005-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump"
            )
            backup.parent.mkdir(mode=0o700, exist_ok=True)
            start = time.perf_counter()
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "agent-ops-tracing-postgres-1",
                        "pg_dump",
                        "-U",
                        "ops_owner",
                        "-d",
                        database,
                        "-Fc",
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            with backup.open("rb") as stream:
                toc = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        "agent-ops-tracing-postgres-1",
                        "pg_restore",
                        "--list",
                    ],
                    stdin=stream,
                    capture_output=True,
                    check=True,
                )
            r["backup"] = {
                "path": str(backup.resolve()),
                "bytes": backup.stat().st_size,
                "mode": oct(backup.stat().st_mode & 0o777),
                "seconds": time.perf_counter() - start,
                "archive_readable": b"support" in toc.stdout,
                "restoration_exercised": False,
            }
            assert r["backup"]["archive_readable"]
            save()
            pid = c.connection.driver_connection.info.backend_pid
            admin.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                    sql.Identifier(database)
                )
            )
            try:
                others = admin.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname=%s AND pid<>%s", (database, pid)
                ).fetchall()
                assert not others, "database became busy during backup; no migration attempted"
                cfg = Config("alembic.ini")
                cfg.attributes.update(connection=c, database_url=str(owner))
                start = time.perf_counter()
                command.upgrade(cfg, "0005_workbench")
                c.commit()
                r["migration_seconds"] = time.perf_counter() - start
                r["version"] = c.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                r["after"] = snapshot(c)
                assert r["before"] == r["after"]
                r["new_live_messages"] = c.execute(
                    text("SELECT count(*) FROM support.messages")
                ).scalar_one()
                r["push_sequence"] = c.execute(
                    text("SELECT value FROM support.push_clock")
                ).scalar_one()
                r["complete"] = True
                c.rollback()
                save()
            finally:
                admin.execute(
                    sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                        sql.Identifier(database)
                    )
                )
                r["connections_restored"] = True
                save()
        engine.dispose()
report["complete"] = True
save()
print(json.dumps({"report": str(report_path), "complete": True}))
