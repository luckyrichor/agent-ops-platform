"""Phase gate: actual fixed SDK/API and non-owner RLS, before P7 search code."""
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from agent_memory.api.app import create_app
from agent_memory.config import Settings
from agent_memory.domain.enums import MemoryType, ScopeKind
from agent_memory.domain.models import MemoryScope
from agent_memory.sdk import MemoryAPIError
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_memory_integration import memory_source_root
from test_workbench import database as ops_database
from testcontainers.community.postgres import PostgresContainer

from agent_ops.customer_memory import CustomerMemory, MemorySigner

database = ops_database


@pytest.fixture(scope='module')
def memory_database():
    root = memory_source_root()
    with PostgresContainer('pgvector/pgvector:pg16', driver='psycopg') as pg:
        config = Config(str(root / 'alembic.ini'))
        config.set_main_option('script_location', str(root / 'migrations'))
        config.set_main_option('sqlalchemy.url', pg.get_connection_url())
        command.upgrade(config, 'head')
        engine = create_engine(pg.get_connection_url())
        with engine.begin() as c:
            c.execute(text("CREATE ROLE customer_runtime LOGIN PASSWORD 'fixture-only' NOSUPERUSER NOBYPASSRLS"))
            c.execute(text('GRANT agent_memory_app TO customer_runtime'))
        url = make_url(pg.get_connection_url()).set(drivername='postgresql+psycopg_async', username='customer_runtime', password='fixture-only').render_as_string(hide_password=False)
        yield url, engine
        engine.dispose()


@pytest.fixture
async def bridge(database, memory_database):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = MemorySigner(key, 'customer-fixture', 'customer-memory-fixture', 300)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    app = create_app(Settings(database_url=memory_database[0], jwt_public_key=public,
        jwt_issuer=signer.issuer, jwt_audience=signer.audience, trace_sample_ratio=0))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://memory')
    bridge = CustomerMemory(database[0], memory_database[0], signer, client)
    yield bridge, app, client
    await bridge.aclose()
    await app.state.engine.dispose()


async def test_lazy_mapping_all_read_paths_isolate_and_tokens_are_bounded(bridge):
    service, _, http = bridge
    async with service.engine.connect() as c:
        assert await c.scalar(text('SELECT count(*) FROM customer_memory.identities')) == 0
    async with service.session(1) as a:
        saved = await a.sdk.remember('fixture preference amber', MemoryType.SEMANTIC,
            MemoryScope(ScopeKind.USER_GLOBAL, None, a.user), idempotency_key='same-key')
        token = service.signer.token(a.tenant, a.user)
        claims = jwt.decode(token, options={'verify_signature':False})
        assert claims['exp'] - claims['iat'] == 300
        assert claims['purpose'] == 'commerce-memory' and claims['aud'] == service.signer.audience
        assert claims['permissions'] == ['memory:read', 'memory:write']
        assert (await a.sdk.get(saved.memory_id)).content == 'fixture preference amber'
    async with service.session(2) as b:
        headers = {'Authorization': 'Bearer ' + service.signer.token(b.tenant, b.user)}
        for path in [f'/v1/memories/{saved.memory_id}', f'/v1/memories/{saved.memory_id}/versions', f'/v1/memories/{saved.memory_id}/lifecycle']:
            assert (await http.get(path, headers=headers)).status_code == 404
        for path in ['/v1/memories/search', '/v1/memories/context']:
            response = await http.post(path, headers=headers, json={'query':'amber'})
            assert response.status_code == 200 and 'amber' not in json.dumps(response.json())
        other = await b.sdk.remember('fixture preference violet', MemoryType.SEMANTIC,
            MemoryScope(ScopeKind.USER_GLOBAL, None, b.user), idempotency_key='same-key')
        assert other.memory_id != saved.memory_id
        with pytest.raises(MemoryAPIError):
            await b.sdk.remember('attack', MemoryType.SEMANTIC,
                MemoryScope(ScopeKind.USER_GLOBAL, None, a.user), idempotency_key='scope-attack')
    for updates in [{'aud':'wrong-purpose'}, {'exp':datetime.now(UTC)-timedelta(seconds=1)}]:
        bad = jwt.encode({**claims, **updates}, service.signer.key, algorithm='RS256')
        assert (await http.get(f'/v1/memories/{saved.memory_id}', headers={'Authorization':'Bearer '+bad})).status_code == 401


async def test_delete_entire_tenant_preserves_other_customer_and_recreates_lazily(bridge):
    service, _, http = bridge
    async with service.session(1) as a:
        saved = await a.sdk.remember('fixture delete amber', MemoryType.SEMANTIC,
            MemoryScope(ScopeKind.USER_GLOBAL, None, a.user), idempotency_key=uuid4().hex)
        old = a.tenant
    async with service.session(2) as b:
        saved_b = await b.sdk.remember('fixture keep violet', MemoryType.SEMANTIC,
            MemoryScope(ScopeKind.USER_GLOBAL, None, b.user), idempotency_key=uuid4().hex)
    assert (await service.delete_all(1))['status'] == 'deleted'
    async with service.session(2) as b:
        assert (await b.sdk.get(saved_b.memory_id)).content == 'fixture keep violet'
    async with service.session(1) as a:
        assert a.tenant != old
        with pytest.raises(MemoryAPIError) as error:
            await a.sdk.get(saved.memory_id)
        assert error.value.status_code == 404
    async with service.memory_engine.begin() as c:
        await c.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"), {'tenant':str(old)})
        for table in service.TABLES:
            assert await c.scalar(text(f'SELECT count(*) FROM {table} WHERE tenant_id=:tenant'), {'tenant':old}) == 0
    assert (await http.get(f'/v1/memories/{saved.memory_id}')).status_code == 401


async def test_purge_failure_blocks_use_until_successful_retry(bridge, monkeypatch):
    from agent_ops.customer_memory import CustomerMemoryError
    service, _, _ = bridge
    async with service.session(1):
        pass
    real_engine = service.memory_engine

    class FailedEngine:
        def begin(self):
            raise CustomerMemoryError('fixture-unavailable')

    monkeypatch.setattr(service, 'memory_engine', FailedEngine())
    with pytest.raises(CustomerMemoryError):
        await service.delete_all(1)
    with pytest.raises(CustomerMemoryError, match='MEMORY_DELETION_IN_PROGRESS'):
        async with service.session(1):
            pytest.fail('deletion gate bypassed')
    monkeypatch.setattr(service, 'memory_engine', real_engine)
    assert (await service.delete_all(1))['status'] == 'deleted'


async def test_delete_http_auth_and_missing_configuration(database, monkeypatch):
    from test_workbench import PASSWORD

    from agent_ops.api import create_app as ops_app
    monkeypatch.setenv('AGENT_OPS_SUPPORT_BACKEND', 'postgres')
    monkeypatch.setenv('AGENT_OPS_DATABASE_URL', database[0])
    monkeypatch.setenv('AGENT_OPS_AUTH_SECRET', 'fixture-' + 'x'*48)
    monkeypatch.setattr(CustomerMemory, 'from_env', lambda _: None)
    app = ops_app(lifecycle_interval=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ops') as http:
        assert (await http.delete('/v1/commerce/memory')).status_code == 401
        for name, expected in [('a1',403), ('c1',200)]:
            auth = await http.post('/auth/login',json={'email':name+'@wb.invalid', 'password':PASSWORD})
            response = await http.delete('/v1/commerce/memory', headers={'Authorization':'Bearer '+auth.json()['access_token']})
            assert response.status_code == expected
            if expected == 200:
                assert response.json() == {'status':'not_configured'}


async def test_mapping_race_and_delete_waits_for_inflight_call(bridge):
    import asyncio
    service, _, _ = bridge
    entered, release = asyncio.Event(), asyncio.Event()
    async def use():
        async with service.session(1) as session:
            tenant = session.tenant
            entered.set()
            await release.wait()
            await session.sdk.remember('fixture concurrent value', MemoryType.SEMANTIC,
                MemoryScope(ScopeKind.USER_GLOBAL, None, session.user), idempotency_key=uuid4().hex)
            return tenant
    task = asyncio.create_task(use())
    await entered.wait()
    deleting = asyncio.create_task(service.delete_all(1))
    await asyncio.sleep(.05)
    assert not deleting.done()
    release.set()
    await task
    assert (await deleting)['status'] == 'deleted'
    async def identify():
        async with service.session(1) as session:
            return session.tenant
    tenants = await asyncio.gather(*(identify() for _ in range(5)))
    assert len(set(tenants)) == 1


async def test_shopping_idempotency_delete_tombstone_and_customer_ownership(bridge):
    from agent_ops.agents import demo_agents
    from agent_ops.catalog_search import CatalogSearch
    from agent_ops.customer_memory import CustomerMemoryError
    from agent_ops.models import CommerceRequest
    from agent_ops.orchestrator import Dispatcher
    from agent_ops.planner import DeterministicPlanner
    from agent_ops.shopping import Shopping
    service, _, _ = bridge
    dispatcher = Dispatcher(demo_agents())
    catalog = CatalogSearch(service.engine.url.render_as_string(hide_password=False))
    shopping = Shopping(catalog, dispatcher, DeterministicPlanner(), service)
    body = CommerceRequest(request='fixture search', category='fixture',budget_cent=3000,region='fixture')
    try:
        first = await shopping.run(body,1)
        assert first.memory_write_status == 'succeeded'
        assert (await shopping.run(body,1)).memory_id == first.memory_id
        with pytest.raises(CustomerMemoryError, match='IDEMPOTENCY_CONFLICT'):
            await shopping.run(body.model_copy(update={'budget_cent':2000}),1)
        other = await shopping.run(body.model_copy(update={'memory_id':first.memory_id,'run_id':uuid4()}),2)
        assert other.memory_read_status == 'read_degraded' and other.memory_id != first.memory_id
        await service.delete_all(1)
        with pytest.raises(CustomerMemoryError, match='MEMORY_OPERATION_PURGED'):
            await shopping.run(body,1)
        import asyncio
        fresh = await asyncio.wait_for(shopping.run(body.model_copy(update={'run_id':uuid4()}),1), timeout=10)
        assert fresh.memory_write_status == 'succeeded'
    finally:
        await dispatcher.aclose()
        await catalog.aclose()


async def test_concurrent_same_operation_and_delete_are_serialized(bridge):
    import asyncio

    from agent_ops.agents import demo_agents
    from agent_ops.catalog_search import CatalogSearch
    from agent_ops.models import CommerceRequest
    from agent_ops.orchestrator import Dispatcher
    from agent_ops.planner import DeterministicPlanner
    from agent_ops.shopping import Shopping
    service, _, _ = bridge
    dispatcher = Dispatcher(demo_agents())
    catalog = CatalogSearch(service.engine.url.render_as_string(hide_password=False))
    shopping = Shopping(catalog,dispatcher,DeterministicPlanner(),service)
    body = CommerceRequest(request='concurrent fixture',category='fixture',budget_cent=3000,region='fixture')
    try:
        first, second = await asyncio.wait_for(asyncio.gather(shopping.run(body,1),shopping.run(body,1)),10)
        assert first.memory_id == second.memory_id
        run = asyncio.create_task(shopping.run(body.model_copy(update={'run_id':uuid4()}),1))
        delete = asyncio.create_task(service.delete_all(1))
        # Either the call finishes before deletion, or it observes the deletion gate.
        outcomes = await asyncio.wait_for(asyncio.gather(run,delete,return_exceptions=True),10)
        assert outcomes[1]['status'] == 'deleted'
    finally:
        await catalog.aclose(); await dispatcher.aclose()


@pytest.mark.parametrize("expired", [False, True])
async def test_ambiguous_memory_write_keeps_frozen_body_and_same_key(bridge, expired):
    from agent_ops.agents import demo_agents
    from agent_ops.catalog_search import CatalogSearch
    from agent_ops.models import CommerceRequest
    from agent_ops.orchestrator import Dispatcher
    from agent_ops.planner import DeterministicPlanner
    from agent_ops.shopping import Shopping
    service, app, original_http = bridge
    dispatcher = Dispatcher(demo_agents())
    catalog = CatalogSearch(service.engine.url.render_as_string(hide_password=False))
    shopping = Shopping(catalog,dispatcher,DeterministicPlanner(),service)
    requests = []
    delegate = httpx.ASGITransport(app=app)

    class LoseResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await delegate.handle_async_request(request)
            if request.url.path == '/v1/memories':
                requests.append((request.headers['idempotency-key'],request.content))
                if len(requests) == 1:
                    await response.aread(); await response.aclose()
                    raise httpx.ReadTimeout('fixture lost response')
            return response

    lossy = httpx.AsyncClient(transport=LoseResponse(),base_url='http://memory')
    service.http = lossy
    body = CommerceRequest(request='ambiguous fixture',category='fixture',budget_cent=3000,region='fixture')
    try:
        assert (await shopping.run(body,1)).memory_write_status == 'degraded'
        if expired:
            from agent_ops.customer_memory import CustomerMemoryError
            async with service.engine.begin() as c:
                await c.execute(text("UPDATE customer_memory.operations SET created_at=now()-interval '8 days' WHERE user_id=1 AND run_id=:run"), {'run':body.run_id})
            with pytest.raises(CustomerMemoryError, match='MEMORY_OPERATION_EXPIRED'):
                await shopping.run(body,1)
            assert len(requests) == 1
            return
        retried = await shopping.run(body,1)
        assert retried.memory_write_status == 'succeeded'
        assert len(requests) == 2 and requests[0] == requests[1]
        async with service.session(1) as session:
            assert (await session.sdk.get(retried.memory_id)).content == json.loads(requests[0][1])['content']
    finally:
        service.http = original_http
        await lossy.aclose(); await catalog.aclose(); await dispatcher.aclose()


def test_signer_private_file_policy_and_key_rotation(tmp_path, monkeypatch):
    from jwt import InvalidSignatureError
    monkeypatch.chdir(tmp_path)
    folder = tmp_path/'.local'; folder.mkdir(mode=0o700)
    path = folder/'signing.pem'
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    path.chmod(0o644)
    with pytest.raises(ValueError): MemorySigner.load(path,'fixture','memory-fixture')
    path.chmod(0o600)
    signer = MemorySigner.load(path,'fixture','memory-fixture')
    token = signer.token(uuid4(),uuid4())
    assert jwt.decode(token,key.public_key(),algorithms=['RS256'],issuer='fixture',audience='memory-fixture')['purpose']=='commerce-memory'
    replacement = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    with pytest.raises(InvalidSignatureError):
        jwt.decode(token,replacement.public_key(),algorithms=['RS256'],issuer='fixture',audience='memory-fixture')
