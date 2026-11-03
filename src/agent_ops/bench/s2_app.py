"""S2 bench API：仅暴露工单创建，用于两实例 HTTP 幂等压测。"""

from __future__ import annotations

import hashlib
import json
import os

from fastapi import FastAPI, Header
from opentelemetry import trace

from agent_ops.support import SupportRequest
from agent_ops.workbench import WorkbenchService

app = FastAPI()
_service = WorkbenchService(os.environ["S1_DATABASE_URL"], trace.get_tracer("s2-bench"))
_CACHE: dict[str, dict[str, str]] = {}


@app.on_event("shutdown")
async def _shutdown() -> None:
    await _service.aclose()


@app.post("/bench/requests")
async def create(body: SupportRequest, idempotency_key: str = Header(...)) -> dict[str, str]:
    cache_key = hashlib.sha256(
        (idempotency_key + ":" + json.dumps(body.model_dump(), sort_keys=True)).encode()
    ).hexdigest()
    if os.environ.get("S2_CACHE") == "1" and cache_key in _CACHE:
        return _CACHE[cache_key]
    ticket = await _service.create(body, idempotency_key, None)
    result = {"ticket_id": ticket.ticket_id}
    if os.environ.get("S2_CACHE") == "1":
        _CACHE[cache_key] = result
    return result
