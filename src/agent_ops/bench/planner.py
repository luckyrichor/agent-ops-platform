"""bench 专用 planner：与生产 LLMPlanner 同样的请求体与校验，端点指向本地桩。

只放宽「必须 https」这一条，且只允许 loopback；不改变计划校验、依赖契约与超时语义。
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx

from agent_ops.models import CommerceRequest, Task
from agent_ops.planner import SYSTEM, Planner, PlanningError, validated_tasks


class StubPlanner:
    mode = "llm"

    def __init__(self, endpoint: str, *, model: str = "deepseek-bench-stub",
                 timeout_seconds: float = 20, concurrency: int = 64,
                 max_attempts: int = 1) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("bench planner requires a loopback http endpoint")
        if not 0 < timeout_seconds <= 60 or not 1 <= max_attempts <= 3:
            raise ValueError("invalid bench planner configuration")
        self.endpoint = endpoint
        self.model: str | None = model
        self.timeout_seconds, self.max_attempts = timeout_seconds, max_attempts
        self._slots = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None

    async def plan(self, request: CommerceRequest) -> list[Task]:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._slots:
                    if self._client is None:
                        self._client = httpx.AsyncClient(
                            timeout=self.timeout_seconds,
                            trust_env=False,
                            limits=httpx.Limits(max_connections=128,
                                                max_keepalive_connections=128),
                        )
                    return await self._request(request)
        except TimeoutError:
            raise PlanningError("LLM_TIMEOUT") from None

    async def _request(self, request: CommerceRequest) -> list[Task]:
        assert self._client is not None
        body = request.model_dump(include={"request", "category", "budget_cent", "region"})
        for attempt in range(self.max_attempts):
            try:
                response = await self._client.post(
                    self.endpoint,
                    headers={
                        "Authorization": "Bearer bench-local",
                        "X-Bench-Request-Id": str(request.run_id),
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
                        ],
                        "thinking": {"type": "disabled"},
                        "max_tokens": 800,
                        "response_format": {"type": "json_object"},
                    },
                )
                if response.status_code in (408, 429) or response.status_code >= 500:
                    if attempt + 1 == self.max_attempts:
                        raise PlanningError(
                            "LLM_RATE_LIMITED" if response.status_code == 429 else "LLM_UNAVAILABLE"
                        )
                elif response.is_error:
                    raise PlanningError("LLM_REQUEST_REJECTED")
                else:
                    try:
                        content = response.json()["choices"][0]["message"]["content"]
                        if not isinstance(content, str):
                            raise TypeError("content type")
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise PlanningError("LLM_INVALID_RESPONSE") from None
                    return validated_tasks(content)
            except httpx.TransportError:
                if attempt + 1 == self.max_attempts:
                    raise PlanningError("LLM_UNAVAILABLE") from None
            await asyncio.sleep(0.2 * (attempt + 1))
        raise PlanningError("LLM_UNAVAILABLE")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def planner_from_env() -> Planner:
    endpoint = os.environ.get("AGENT_OPS_BENCH_STUB_ENDPOINT", "")
    if not endpoint:
        raise ValueError("AGENT_OPS_BENCH_STUB_ENDPOINT is required")
    return StubPlanner(
        endpoint,
        timeout_seconds=float(os.environ.get("AGENT_OPS_BENCH_TIMEOUT_SECONDS", "20")),
        concurrency=int(os.environ.get("AGENT_OPS_BENCH_PLANNER_CONCURRENCY", "64")),
        max_attempts=int(os.environ.get("AGENT_OPS_BENCH_MAX_ATTEMPTS", "1")),
    )
