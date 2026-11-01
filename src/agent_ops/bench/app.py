"""bench 专用 API：复制现有鉴权/编排代码，模型指向本地桩。

只做隔离实例，不改生产行为：默认关闭顾客记忆（无 memory 环境变量），
每个请求仍走真实 planner 校验与四角色编排。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from agent_ops.api import create_app


async def transport_hold(seconds: float, interval: float = 0.2) -> AsyncIterator[str]:
    """显式 fixture：固定时长内持续发心跳，用于传输连接负载，不代表业务完成率。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    yield "event: started\ndata: {}\n\n"
    while loop.time() - started < seconds:
        await asyncio.sleep(interval)
        yield "event: heartbeat\ndata: {}\n\n"
    yield "event: result\ndata: " + json.dumps({"fixture": "transport_hold"}) + "\n\n"


def bench_app() -> FastAPI:
    from agent_ops.bench.planner import planner_from_env

    app = create_app(
        planner=planner_from_env(),
        baseline_without_auth=os.environ.get("AGENT_OPS_BENCH_AUTH", "off") == "off",
    )

    @app.get("/bench/hold")
    async def hold(seconds: float = 240) -> StreamingResponse:
        if not 0 < seconds <= 900:
            raise ValueError("hold seconds out of range")
        return StreamingResponse(
            transport_hold(seconds), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return app
