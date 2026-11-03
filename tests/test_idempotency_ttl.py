"""确定性并发守卫：TTL 过期后旧请求的完成写入不得覆盖新结果。"""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest
from opentelemetry.trace import get_tracer
from sqlalchemy import text

from agent_ops.support import SupportRequest
from agent_ops.workbench import WorkbenchService

URL = os.environ.get("S1_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="S1_DATABASE_URL required for idempotency TTL tests")


@pytest.fixture
async def service():
    instance = WorkbenchService(URL, get_tracer("s2-ttl-test"))
    yield instance
    await instance.aclose()


async def test_stale_finish_after_expiry_cannot_overwrite(service):
    request = SupportRequest(message="s2 deterministic guard", order_id=None)
    key = f"guard-{uuid4().hex}"
    first = await service.create(request, key, None)
    first_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()

    async with service.engine.begin() as c:
        await c.execute(
            text("UPDATE support.idempotency_keys SET expires_at=now()-interval '1 second'"
                 " WHERE scope='support.request' AND key=:k"), {"k": key})

    second = await service.create(request, key, None)
    assert second.ticket_id != first.ticket_id

    # 模拟旧请求在新记录写入后才完成：带旧 request_hash 的更新必须影响 0 行。
    async with service.engine.begin() as c:
        result = await c.execute(
            text("""UPDATE support.idempotency_keys SET response_snapshot=CAST(:r AS jsonb)
            WHERE scope='support.request' AND key=:k AND request_hash=:h
            AND response_snapshot IS NULL"""),
            {"r": first.model_dump_json(), "k": key, "h": first_hash})
        assert result.rowcount == 0
        snapshot = await c.scalar(
            text("SELECT response_snapshot->>'ticket_id' FROM support.idempotency_keys"
                 " WHERE scope='support.request' AND key=:k"), {"k": key})
    assert snapshot == second.ticket_id


async def test_expired_record_is_refreshed_atomically(service):
    request = SupportRequest(message="s2 refresh guard", order_id=None)
    key = f"refresh-{uuid4().hex}"
    await service.create(request, key, None)
    async with service.engine.begin() as c:
        await c.execute(
            text("UPDATE support.idempotency_keys SET expires_at=now()"
                 " WHERE scope='support.request' AND key=:k"), {"k": key})
    third = await service.create(request, key, None)
    async with service.engine.connect() as c:
        row = (await c.execute(
            text("SELECT expires_at>now() AS fresh, response_snapshot->>'ticket_id' AS tid"
                 " FROM support.idempotency_keys WHERE scope='support.request' AND key=:k"),
            {"k": key})).one()
    assert row.fresh is True
    assert row.tid == third.ticket_id
