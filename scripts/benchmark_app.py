"""Loopback benchmark factory only. The normal agent_ops.api:create_app always requires login."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from agent_ops.api import create_app as api_app


def create_app():
    mode = os.environ['AGENT_OPS_BENCHMARK_AUTH']
    if mode not in {'on', 'off'}:
        raise ValueError('explicit benchmark auth on/off required')
    app = api_app(baseline_without_auth=mode == 'off')
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_lifespan(app):
        async with lifespan(app):
            if mode == 'on':
                roster = json.loads(Path(os.environ['AGENT_OPS_BENCHMARK_ROSTER']).read_text())
                await app.state.auth.create_account(roster['email'], roster['password'], 'customer', 1)
            yield
    app.router.lifespan_context = fixture_lifespan
    return app
