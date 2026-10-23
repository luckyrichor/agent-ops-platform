"""P0 one-command local stack and safe environment report; secrets never printed."""

import json
import os
import platform
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

root = Path(".local")
root.mkdir(exist_ok=True)
root.chmod(0o700)
secretfile = root / "stack.env"
if not secretfile.exists():
    secretfile.touch(mode=0o600)
    secretfile.write_text(
        "".join(
            f"{k}={secrets.token_urlsafe(32)}\n"
            for k in ("POSTGRES_PASSWORD", "OPS_APP_PASSWORD", "REDIS_PASSWORD", "GRAFANA_PASSWORD")
        )
    )
secretfile.chmod(0o600)
values = dict(line.split("=", 1) for line in secretfile.read_text().splitlines() if line)
# P3: the shared SUPPORT_HUMAN_TOKEN is retired; an old value in stack.env is simply unused.
if "AGENT_OPS_AUTH_SECRET" not in values:
    values["AGENT_OPS_AUTH_SECRET"] = secrets.token_urlsafe(48)
    with secretfile.open("a") as stream:
        stream.write("AGENT_OPS_AUTH_SECRET=" + values["AGENT_OPS_AUTH_SECRET"] + "\n")
compose = ["docker", "compose", "--env-file", str(secretfile), "-f", "compose.yaml"]
subprocess.run(
    ["docker", "volume", "create", "agent-ops-tracing-data"], check=True, stdout=subprocess.DEVNULL
)
subprocess.run([*compose, "up", "-d", "--wait", "--wait-timeout", "180"], check=True)
url = f"postgresql+psycopg://ops_owner:{values['POSTGRES_PASSWORD']}@127.0.0.1:55433/agent_ops"
config = Config("alembic.ini")
config.attributes["database_url"] = url
command.upgrade(config, "head")
with psycopg.connect(url.replace("postgresql+psycopg:", "postgresql:")) as conn:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='ops_runtime'").fetchone():
        conn.execute(
            sql.SQL(
                "CREATE ROLE ops_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
            ).format(sql.Literal(values["OPS_APP_PASSWORD"]))
        )
    conn.execute("GRANT agent_ops_app TO ops_runtime")
appurl = f"postgresql+psycopg://ops_runtime:{values['OPS_APP_PASSWORD']}@127.0.0.1:55433/agent_ops"
appfile = root / "application.env"
appfile.touch(mode=0o600)
appfile.write_text(
    f"AGENT_OPS_SUPPORT_BACKEND=postgres\nAGENT_OPS_DATABASE_URL={appurl}\nAGENT_OPS_AUTH_SECRET={values['AGENT_OPS_AUTH_SECRET']}\nAGENT_OPS_PLANNER=deterministic\nAGENT_OPS_TRACE_EXPORTER=otlp\nAGENT_OPS_OTLP_ENDPOINT=http://127.0.0.1:14318/v1/traces\n"
)
appfile.chmod(0o600)
health = {}
with httpx.Client(timeout=5, trust_env=False) as client:
    for name, endpoint in {
        "jaeger": "http://127.0.0.1:13133/status",
        "collector": "http://127.0.0.1:13134/",
        "prometheus": "http://127.0.0.1:19091/-/ready",
        "grafana": "http://127.0.0.1:13001/api/health",
    }.items():
        for attempt in range(60):
            try:
                response = client.get(endpoint)
                if response.is_success:
                    health[name] = response.status_code
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("stack health check failed: " + name)
    grafana = client.get("http://127.0.0.1:13001/api/health").json()
ids = subprocess.check_output([*compose, "ps", "-q"], text=True).split()
containers = []
for cid in ids:
    info = json.loads(subprocess.check_output(["docker", "inspect", cid]))[0]
    containers.append(
        {
            "service": info["Config"]["Labels"]["com.docker.compose.service"],
            "image": info["Config"]["Image"],
            "image_id": info["Image"],
            "state": info["State"]["Status"],
            "memory_limit": info["HostConfig"]["Memory"],
            "nano_cpus": info["HostConfig"]["NanoCpus"],
            "ports": info["NetworkSettings"]["Ports"],
        }
    )
with psycopg.connect(appurl.replace("postgresql+psycopg:", "postgresql:")) as conn:
    pg_version = conn.execute("SHOW server_version").fetchone()[0]
    non_owner = conn.execute(
        "SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole FROM pg_roles WHERE rolname=current_user"
    ).fetchone()[0]
redis_version = subprocess.check_output(
    [*compose, "exec", "-T", "redis", "redis-cli", "INFO", "server"], text=True
)
report = {
    "generated_at": datetime.now(UTC).isoformat(),
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"])),
    "host": {
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "meminfo": {
            l.split(":")[0]: l.split(":")[1].strip()
            for l in Path("/proc/meminfo").read_text().splitlines()
            if l.startswith(("MemTotal:", "MemAvailable:"))
        },
    },
    "versions": {
        "postgres": pg_version,
        "redis": next(
            l.split(":")[1].strip()
            for l in redis_version.splitlines()
            if l.startswith("redis_version:")
        ),
        "grafana": grafana["version"],
        "docker": subprocess.check_output(["docker", "--version"], text=True).strip(),
        "compose": subprocess.check_output(
            ["docker", "compose", "version", "--short"], text=True
        ).strip(),
    },
    "containers": containers,
    "http_health": health,
    "runtime_not_superuser": non_owner,
    "baseline_tags": {
        tag: subprocess.check_output(["git", "rev-parse", tag], text=True).strip()
        for tag in ["baseline-v0", "baseline-b0"]
    },
}
folder = Path("docs/environments") / ("p0-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
folder.mkdir(parents=True, exist_ok=False)
(folder / "environment.json").write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {
            "environment_report": str(folder / "environment.json"),
            "services": len(containers),
            "health": health,
            "app_environment_file": str(appfile),
        }
    )
)
