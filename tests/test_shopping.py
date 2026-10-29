"""P7 live catalog contracts; cache seam must not change results or hide writes."""
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from test_workbench import database as ops_database

from agent_ops.agents import demo_agents
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.commerce_backend import CommerceBackend, PlaceOrder
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import DeterministicPlanner
from agent_ops.shopping import Shopping

database = ops_database


@pytest.fixture
async def catalog(database):
    with database[1].begin() as c:
        c.execute(text("INSERT INTO commerce.categories(id,name) VALUES(901,'Fixture kitchen') ON CONFLICT DO NOTHING"))
        c.execute(text('''INSERT INTO commerce.products(id,sku,title,category_id,price_cents,freight_cents,status,attrs) VALUES
          (901,'SHOP901','Amber electric kettle',901,1200,100,'active','{"color":"amber"}'),
          (902,'SHOP902','Violet electric kettle',901,900,200,'active','{"color":"violet"}'),
          (903,'SHOP903','Sold out kettle',901,100,0,'active','{}'),
          (904,'SHOP904','Inactive kettle',901,10,0,'inactive','{}') ON CONFLICT DO NOTHING'''))
        c.execute(text('INSERT INTO commerce.inventory(product_id,stock,reserved) VALUES(901,10,0),(902,10,0),(903,0,0),(904,10,0) ON CONFLICT DO NOTHING'))
    search = CatalogSearch(database[0])
    yield search
    await search.aclose()


async def test_full_text_structured_filters_pagination_and_cache_seam(catalog):
    query = CatalogQuery(query='electric kettles', category_id=901, max_price_cent=1500)
    off = await catalog.search(query)
    assert [p['id'] for p in off] == [902,901]
    catalog.cache.enabled = True
    assert await catalog.search(query) == off
    assert [p['id'] for p in await catalog.search(query.model_copy(update={'attributes':{'color':'amber'}}))] == [901]
    assert [p['id'] for p in await catalog.search(query.model_copy(update={'min_price_cent':1000}))] == [901]
    assert [p['id'] for p in await catalog.search(query.model_copy(update={'offset':1,'limit':1}))] == [901]
    assert await catalog.search(CatalogQuery(query="'; DROP TABLE commerce.products; --")) == []
    assert await catalog.search(CatalogQuery(query='unfindable fixture')) == []
    with pytest.raises(ValueError):
        CatalogQuery(limit=101)


async def test_real_graph_uses_database_price_freight_and_reservations(catalog, database):
    dispatcher = Dispatcher(demo_agents())
    shopping = Shopping(catalog, dispatcher, DeterministicPlanner(), None)
    request = CommerceRequest(request='electric kettle',category='kitchen',category_id=901,budget_cent=1400,region='上海')
    commerce = CommerceBackend(database[0])
    try:
        off = await shopping.run(request,1)
        assert off.recommendation['selected'] == {'id':'902','title':'Violet electric kettle','total_cent':1100}
        assert off.tasks[0].result['source'] == 'postgresql-catalog'
        catalog.cache.enabled = True
        on = await shopping.run(request.model_copy(update={'run_id':uuid4()}),1)
        assert on.recommendation == off.recommendation
        order = await commerce.place_order(PlaceOrder(user_id=1,lines=[{'product_id':902,'quantity':10}]),uuid4().hex)
        changed = await shopping.run(request.model_copy(update={'run_id':uuid4()}),1)
        assert changed.recommendation['selected']['id'] == '901'
        await commerce.cancel_order(order['order_id'],1,1)
        assert (await shopping.run(request.model_copy(update={'run_id':uuid4()}),1)).recommendation == off.recommendation
    finally:
        await commerce.aclose()
        await dispatcher.aclose()


async def test_http_search_runs_and_stream_use_real_products(catalog, database, monkeypatch):
    from test_workbench import PASSWORD

    from agent_ops.api import create_app
    monkeypatch.setenv('AGENT_OPS_SUPPORT_BACKEND','postgres')
    monkeypatch.setenv('AGENT_OPS_DATABASE_URL',database[0])
    monkeypatch.setenv('AGENT_OPS_AUTH_SECRET','fixture-'+'x'*48)
    app = create_app(lifecycle_interval=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://ops') as http:
        assert (await http.get('/products/search',params={'query':'kettles'})).status_code == 200
        assert (await http.get('/products/search',params={'limit':1001})).status_code == 422
        body = {'request':'electric kettle','category':'kitchen','category_id':901,'budget_cent':1400,'region':'上海'}
        assert (await http.post('/v1/commerce/runs',json=body)).status_code == 401
        auth = await http.post('/auth/login',json={'email':'c1@wb.invalid','password':PASSWORD})
        headers = {'Authorization':'Bearer '+auth.json()['access_token']}
        result = await http.post('/v1/commerce/runs',json=body,headers=headers)
        assert result.status_code == 200
        assert result.json()['recommendation']['selected']['id'] == '902'
        streamed = await http.post('/v1/commerce/stream',json=body,headers=headers)
        assert 'event: result' in streamed.text and 'postgresql-catalog' in streamed.text
