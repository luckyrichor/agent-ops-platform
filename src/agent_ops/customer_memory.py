"""Customer-scoped service identities. No platform bearer crosses the service boundary."""
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
from agent_memory.sdk import MemoryClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


class CustomerMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemorySigner:
    key: RSAPrivateKey
    issuer: str
    audience: str
    ttl: int = 300

    def __post_init__(self) -> None:
        if not 60 <= self.ttl <= 600 or not self.issuer or not self.audience or self.key.key_size < 2048:
            raise ValueError('INVALID_MEMORY_SIGNER')

    def token(self, tenant: UUID, user: UUID) -> str:
        now = datetime.now(UTC)
        return jwt.encode({'iss':self.issuer, 'aud':self.audience, 'sub':str(user),
            'tenant_id':str(tenant), 'purpose':'commerce-memory', 'roles':[],
            'permissions':['memory:read', 'memory:write'], 'allowed_workspace_ids':[],
            'iat':now, 'nbf':now, 'exp':now+timedelta(seconds=self.ttl), 'jti':str(uuid4())},
            self.key, algorithm='RS256')

    @classmethod
    def load(cls, path: Path, issuer: str, audience: str, ttl: int = 300) -> 'MemorySigner':
        private = (Path.cwd()/'.local').resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(private) or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('MEMORY_KEY_REQUIRES_PRIVATE_LOCAL_FILE')
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, RSAPrivateKey):
            raise TypeError('INVALID_MEMORY_SIGNER')
        return cls(key, issuer, audience, ttl)


@dataclass
class CustomerSession:
    sdk: MemoryClient
    tenant: UUID
    user: UUID
    connection: AsyncConnection
    generation: int


class CustomerMemory:
    # Version-pinned administrative adapter. Dedicated DB, no external workers.
    TABLES = ('evidence', 'memory_embeddings', 'memory_versions', 'memories',
              'events', 'outbox_messages', 'jobs', 'idempotency_records', 'audit_logs')

    def __init__(self, platform_url: str, memory_url: str, signer: MemorySigner,
                 http: httpx.AsyncClient) -> None:
        self.engine = create_async_engine(platform_url, pool_pre_ping=True)
        self.memory_engine = create_async_engine(memory_url, pool_pre_ping=True)
        self.signer, self.http = signer, http

    @classmethod
    def from_env(cls, platform_url: str) -> 'CustomerMemory | None':
        names = ('AGENT_OPS_CUSTOMER_MEMORY_URL', 'AGENT_OPS_CUSTOMER_MEMORY_PURGE_DATABASE_URL',
                 'AGENT_OPS_CUSTOMER_MEMORY_KEY', 'AGENT_OPS_CUSTOMER_MEMORY_ISSUER',
                 'AGENT_OPS_CUSTOMER_MEMORY_AUDIENCE')
        values = [os.environ.get(name, '') for name in names]
        if not any(values):
            return None
        if not all(values):
            raise ValueError('CUSTOMER_MEMORY_CONFIGURATION_INCOMPLETE')
        url, database, key, issuer, audience = values
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('CUSTOMER_MEMORY_REQUIRES_LOOPBACK')
        signer = MemorySigner.load(Path(key), issuer, audience,
            int(os.environ.get('AGENT_OPS_CUSTOMER_MEMORY_TTL', '300')))
        return cls(platform_url, database, signer,
            httpx.AsyncClient(base_url=url, timeout=5, trust_env=False, follow_redirects=False))

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()
        await self.memory_engine.dispose()

    @asynccontextmanager
    async def session(self, user_id: int) -> AsyncIterator[CustomerSession]:
        if user_id <= 0:
            raise CustomerMemoryError('MEMORY_USER_REQUIRED')
        while True:
            # Commit identity allocation before taking the call lock. UUID unique
            # key updates otherwise upgrade the lock and block the durable child
            # operation's FK check on a separate connection.
            async with self.engine.begin() as c:
                await c.execute(text("""INSERT INTO customer_memory.identities(user_id,tenant_id,memory_user_id)
                  VALUES(:id,:tenant,:user) ON CONFLICT DO NOTHING"""),
                  {'id':user_id, 'tenant':uuid4(), 'user':uuid4()})
                row = (await c.execute(text('SELECT * FROM customer_memory.identities WHERE user_id=:id FOR UPDATE'), {'id':user_id})).mappings().one()
                if row['state'] != 'active':
                    raise CustomerMemoryError('MEMORY_DELETION_IN_PROGRESS')
                if row['tenant_id'] is None:
                    await c.execute(text('UPDATE customer_memory.identities SET tenant_id=:tenant,memory_user_id=:user WHERE user_id=:id'), {'tenant':uuid4(), 'user':uuid4(), 'id':user_id})
            async with self.engine.begin() as c:
                row = (await c.execute(text('SELECT * FROM customer_memory.identities WHERE user_id=:id FOR NO KEY UPDATE'), {'id':user_id})).mappings().one()
                if row['state'] != 'active':
                    raise CustomerMemoryError('MEMORY_DELETION_IN_PROGRESS')
                if row['tenant_id'] is None:
                    continue  # A delete completed between allocation and locking.
                tenant, user = row['tenant_id'], row['memory_user_id']
                yield CustomerSession(MemoryClient(self.http, token=self.signer.token(tenant, user)),
                                      tenant, user, c, row['generation'])
                return

    async def delete_all(self, user_id: int) -> dict[str, Any]:
        if user_id <= 0:
            raise CustomerMemoryError('MEMORY_USER_REQUIRED')
        request_id = uuid4()
        await self._audit_delete(user_id, request_id, 'started')
        try:
            result = await self._delete_all(user_id)
        except Exception:
            await self._audit_delete(user_id, request_id, 'failed')
            raise
        await self._audit_delete(user_id, request_id, 'completed')
        return result

    async def _audit_delete(self, user_id: int, request_id: UUID, outcome: str) -> None:
        # Survives tenant erasure; contains no memory body, token or tenant UUID.
        async with self.engine.begin() as c:
            await c.execute(text("""INSERT INTO customer_memory.deletion_audit
                (request_id,user_id,outcome) VALUES(:request,:user,:outcome)"""),
                {'request':request_id,'user':user_id,'outcome':outcome})

    async def _delete_all(self, user_id: int) -> dict[str, Any]:
        # Commit the gate before touching a different database. A crash is retryable
        # and cannot report success while leaving old tenant data accessible.
        async with self.engine.begin() as c:
            row = (await c.execute(text('SELECT * FROM customer_memory.identities WHERE user_id=:id FOR UPDATE'), {'id':user_id})).mappings().first()
            if row is None or row['tenant_id'] is None:
                return {'status':'deleted', 'deleted_tenants':0}
            tenant = row['tenant_id']
            await c.execute(text("UPDATE customer_memory.identities SET state='deleting' WHERE user_id=:id"), {'id':user_id})
        async with self.memory_engine.begin() as c:
            role = (await c.execute(text("SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb, pg_has_role(current_user,'agent_memory_app','MEMBER') AS runtime_member FROM pg_roles WHERE rolname=current_user"))).one()
            if role.rolsuper or role.rolbypassrls or role.rolcreaterole or role.rolcreatedb or role.runtime_member:
                raise CustomerMemoryError('MEMORY_PURGE_ROLE_UNSAFE')
            await c.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"), {'tenant':str(tenant)})
            # Deferrable current-version FK; successor references are within tenant.
            await c.execute(text('UPDATE memories SET current_version_id=NULL,successor_id=NULL WHERE tenant_id=:tenant'), {'tenant':tenant})
            for table in self.TABLES:
                await c.execute(text(f'DELETE FROM {table} WHERE tenant_id=:tenant'), {'tenant':tenant})
        async with self.engine.begin() as c:
            current = (await c.execute(text('SELECT tenant_id FROM customer_memory.identities WHERE user_id=:id FOR UPDATE'), {'id':user_id})).one()
            if current.tenant_id == tenant:
                await c.execute(text("UPDATE customer_memory.operations SET state='purged',memory_content=NULL,result=NULL,request_hash='' WHERE user_id=:id"), {'id':user_id})
                await c.execute(text("UPDATE customer_memory.identities SET state='active',tenant_id=NULL,memory_user_id=NULL,generation=generation+1 WHERE user_id=:id"), {'id':user_id})
        return {'status':'deleted', 'deleted_tenants':1}

    async def operation(self, user_id: int, run_id: UUID, digest: str) -> dict[str, Any]:
        # Separate durable commit while the caller holds the identity's NO KEY
        # UPDATE lock: ambiguous network writes retain the same content on retry.
        async with self.engine.begin() as c:
            await c.execute(text("""INSERT INTO customer_memory.operations(user_id,run_id,request_hash,state)
              VALUES(:user,:run,:hash,'pending') ON CONFLICT DO NOTHING"""),
              {'user':user_id, 'run':run_id, 'hash':digest})
            row = (await c.execute(text('SELECT * FROM customer_memory.operations WHERE user_id=:user AND run_id=:run'), {'user':user_id, 'run':run_id})).mappings().one()
            if row['state'] == 'purged':
                raise CustomerMemoryError('MEMORY_OPERATION_PURGED')
            if row['request_hash'] != digest:
                raise CustomerMemoryError('IDEMPOTENCY_CONFLICT')
            if row['state'] != 'succeeded' and datetime.now(UTC)-row['created_at'] >= timedelta(days=7):
                raise CustomerMemoryError('MEMORY_OPERATION_EXPIRED')
            return dict(row)

    async def save_content(self, user_id: int, run_id: UUID, content: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(text('UPDATE customer_memory.operations SET memory_content=:content WHERE user_id=:user AND run_id=:run'), {'user':user_id, 'run':run_id, 'content':content})

    async def finish(self, user_id: int, run_id: UUID, result: dict[str, Any]) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("UPDATE customer_memory.operations SET state='succeeded',result=CAST(:result AS jsonb) WHERE user_id=:user AND run_id=:run"), {'user':user_id, 'run':run_id, 'result':json.dumps(result)})


def memory_router(service: CustomerMemory | None, *, customer: Any) -> Any:
    from typing import Annotated

    from fastapi import APIRouter, Depends, HTTPException
    from sqlalchemy.exc import SQLAlchemyError

    from agent_ops.auth import Principal

    router = APIRouter(prefix='/v1/commerce')
    Customer = Annotated[Principal, Depends(customer)]

    @router.delete('/memory')
    async def delete_memory(principal: Customer) -> dict[str, Any]:
        if service is None:
            return {'status':'not_configured'}
        assert principal.customer_user_id is not None
        try:
            return await service.delete_all(principal.customer_user_id)
        except (CustomerMemoryError, SQLAlchemyError, httpx.HTTPError):
            raise HTTPException(503, 'MEMORY_DELETE_UNAVAILABLE') from None

    return router
