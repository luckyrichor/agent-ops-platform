import asyncio
import socket

import httpx
import pytest
import uvicorn

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.orchestrator import Dispatcher


@pytest.mark.asyncio
async def test_real_http_disconnect_propagates_and_other_requests_finish():
    started, cancelled = asyncio.Event(), asyncio.Event()
    handlers = demo_agents()
    original = handlers["catalog"]
    async def catalog(request, inputs):
        if request.request == "wait":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(request, inputs)
    handlers["catalog"] = catalog
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(Dispatcher(handlers)), log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.005)
        body = {"request": "wait", "category": "kettle", "budget_cent": 5000, "region": "上海"}
        async with httpx.AsyncClient(base_url=address, timeout=3, trust_env=False) as client:
            async with client.stream("POST", "/v1/commerce/stream", json=body) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line == "event: started":
                        break
                await asyncio.wait_for(started.wait(), 1)
                replies = await asyncio.gather(*[
                    client.post("/v1/commerce/runs", json={**body, "request": "normal"})
                    for _ in range(16)])
                assert all(reply.json()["outcome"] == "succeeded" for reply in replies)
            await asyncio.wait_for(cancelled.wait(), 2)
            async with client.stream("POST", "/v1/commerce/stream",
                                     json={**body, "request": "normal"}) as response:
                text = (await response.aread()).decode()
                assert 'event: result' in text and '"outcome": "succeeded"' in text
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 3)
        sock.close()
