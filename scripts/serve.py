"""Explicit loopback development server; PG is normal mode, memory is B0 switch."""

import argparse
import os
from pathlib import Path

import uvicorn

p = argparse.ArgumentParser()
p.add_argument("--backend", choices=("postgres", "memory"), default="postgres")
p.add_argument("--port", type=int, default=8003)
p.add_argument("--env-file", type=Path, default=Path(".local/application.env"))
a = p.parse_args()
if a.backend == "postgres":
    env = a.env_file
    if not env.exists():
        p.error("run bash scripts/start-stack.sh first")
    os.environ.update(dict(line.split("=", 1) for line in env.read_text().splitlines() if line))
os.environ["AGENT_OPS_SUPPORT_BACKEND"] = a.backend
os.environ.setdefault("AGENT_OPS_PLANNER", "deterministic")
uvicorn.run("agent_ops.api:create_app", factory=True, host="127.0.0.1", port=a.port)
