"""Actual SDK -> memory ASGI API -> migrated PostgreSQL integration."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from agent_memory.api.app import create_app
from agent_memory.config import Settings
from agent_memory.domain.enums import MemoryType, ScopeKind
from agent_memory.domain.models import MemoryScope
from agent_memory.sdk import MemoryAPIError, MemoryClient
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher


@pytest.mark.asyncio
async def test_actual_memory_api_read_write_replay_conflict_and_archive(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "agent-memory"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    tenant, user = uuid4(), uuid4()
    now = datetime.now(UTC)
    token = jwt.encode({"iss": "ops-test", "aud": "memory-api", "sub": str(user),
        "tenant_id": str(tenant), "roles": [],
        "permissions": ["memory:read", "memory:write", "memory:delete", "memory:archive"],
        "allowed_workspace_ids": ["commerce"], "iat": now, "exp": now+timedelta(minutes=5)},
        private, algorithm="RS256")
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        monkeypatch.chdir(root)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("CREATE ROLE ops_test LOGIN PASSWORD 'integration-only' "
                                    "NOSUPERUSER NOBYPASSRLS"))
            connection.execute(text("GRANT agent_memory_app TO ops_test"))
        engine.dispose()
        app_url = make_url(url).set(drivername="postgresql+psycopg_async",
                                    username="ops_test", password="integration-only")
        app = create_app(Settings(database_url=app_url.render_as_string(hide_password=False),
                                  jwt_public_key=public, jwt_issuer="ops-test", jwt_audience="memory-api"))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://memory") as http:
                sdk = MemoryClient(http, token=token)
                preference = await sdk.remember('{"budget_cent":3000}', MemoryType.SEMANTIC,
                    MemoryScope(ScopeKind.WORKSPACE, "commerce", None), idempotency_key="preference")
                request = CommerceRequest(request="水壶", category="kettle", budget_cent=5000,
                                          region="上海", memory_id=preference.memory_id)
                service = MemoryCommerce(Dispatcher(demo_agents()), sdk)
                first = await service.run(request)
                replay = await service.run(request)
                assert first.memory_id == replay.memory_id
                assert first.memory_status == "available"
                assert first.recommendation["selected"] is None
                with pytest.raises(MemoryAPIError) as conflict:
                    await service.run(request.model_copy(update={"request": "changed"}))
                assert conflict.value.status_code == 409
                await sdk.archive(preference.memory_id, expected_revision=1)
                fallback = await service.run(request.model_copy(update={"run_id": uuid4()}))
                assert fallback.memory_status == "read_degraded"
                assert fallback.recommendation["selected"]["id"] == "kettle-basic"
                assert fallback.memory_id is not None
                search = await service.run(request.model_copy(update={"run_id": uuid4(),
                                                                      "memory_id": None}))
                assert search.memory_hit_count == 0  # archived preference cannot enter retrieval
                assert search.memory_status == "available"
        finally:
            await app.state.engine.dispose()
