"""P6 ephemeral-key signatures, actual PG ledger, replay/concurrency and gateway failure bounds."""
import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from opentelemetry.trace import get_tracer
from sqlalchemy import text
from test_workbench import database as workbench_database

database = workbench_database  # Register the shared actual-PG fixture without importing its tests.

from agent_ops.auth import Principal
from agent_ops.commerce_backend import CommerceBackend, PlaceOrder
from agent_ops.payment_api import notification_app
from agent_ops.payment_gateway import (
    Gateway,
    MerchantBinding,
    PaymentConfig,
    PaymentError,
    canonical,
    cents,
)
from agent_ops.payments import Payments
from agent_ops.support import SupportRequest
from agent_ops.workbench import WorkbenchService


@pytest.fixture(scope='module')
def keys():
    merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = PaymentConfig('fixture-app', 'fixture-merchant', 'https://gateway.invalid/service',
                           'http://notify.invalid/payments/notify', merchant, provider.public_key(), .01, 2)
    return config, provider


@pytest.fixture
async def payment(database, keys):
    url, owner, _ = database
    with owner.begin() as c:
        c.execute(text("INSERT INTO commerce.categories(id,name) VALUES(801,'payment-fixture') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO commerce.products(id,sku,title,category_id,price_cents,freight_cents,status) VALUES(801,'P-FIXTURE','Fixture',801,100,0,'active') ON CONFLICT DO NOTHING"))
        c.execute(text("SELECT setval(pg_get_serial_sequence('commerce.orders','id'),GREATEST((SELECT coalesce(max(id),1) FROM commerce.orders),1000))"))
        c.execute(text('INSERT INTO commerce.inventory(product_id,stock,reserved) VALUES(801,1000,0) ON CONFLICT DO NOTHING'))
    support = WorkbenchService(url, get_tracer('p6-test'))
    service = Payments(url, keys[0], support=support)
    commerce = CommerceBackend(url)
    order = await commerce.place_order(PlaceOrder(user_id=1, lines=[{'product_id':801,'quantity':2}]), uuid4().hex)
    await service.create(order['order_id'], 1)
    async with service.engine.connect() as c:
        p = (await c.execute(text('SELECT * FROM payments.payments WHERE order_id=:id'), {'id':order['order_id']})).mappings().one()
    yield service, commerce, dict(p), keys[1]
    await service.aclose(); await commerce.aclose(); await support.aclose()


def notice(payment, **updates):
    _, _, row, key = payment
    params = {'notify_id':uuid4().hex,'sign_type':'RSA2','app_id':'fixture-app',
              'seller_id':'fixture-merchant','out_trade_no':row['merchant_no'],
              'trade_no':'T'+str(row['order_id']),'trade_status':'TRADE_SUCCESS','total_amount':'2.00'}
    params.update(updates)
    params['sign'] = base64.b64encode(key.sign(canonical(params, notification=True), padding.PKCS1v15(), hashes.SHA256())).decode()
    return params


async def test_five_replays_and_two_concurrent_workers_capture_once(payment, database):
    service, commerce, row, _ = payment
    second = Payments(database[0], service.config)
    async with service.engine.connect() as c:
        before = (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one()
    params = notice(payment)
    try:
        await asyncio.gather(service.notify(params), second.notify(params))
        for _ in range(3): await service.notify(params)
    finally: await second.aclose()
    async with service.engine.connect() as c:
        after = (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one()
        assert await c.scalar(text("SELECT count(*) FROM payments.audit WHERE order_id=:id AND code='PAYMENT_CAPTURED'"), {'id':row['order_id']}) == 1
    assert before.stock-after.stock == 2 and before.reserved-after.reserved == 2
    assert (await commerce.order(row['order_id'],1))['status']=='paid'


@pytest.mark.parametrize('change,code',[
    ({'total_amount':'2.01'},'PAYMENT_AMOUNT_MISMATCH'),
    ({'seller_id':'different'},'PAYMENT_MERCHANT_MISMATCH'),
    ({'app_id':'different'},'PAYMENT_APP_MISMATCH'),
    ({'out_trade_no':'unknown'},'PAYMENT_ORDER_UNKNOWN'),
    ({'trade_status':'unknown'},'PAYMENT_STATUS_INVALID'),
])
async def test_signed_semantic_mismatches_rejected_and_audited(payment, change, code):
    service, commerce, row, _ = payment
    with pytest.raises(PaymentError,match=code): await service.notify(notice(payment,**change))
    assert (await commerce.order(row['order_id'],1))['status']=='pending_payment'
    async with service.engine.connect() as c:
        assert await c.scalar(text('SELECT count(*) FROM payments.audit WHERE code=:code'),{'code':code})>=1


async def test_wrong_signature_duplicate_fields_and_http_bounds(payment):
    service, _, _, _ = payment
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=notification_app(service)),base_url='http://fixture')
    async with client:
        params=notice(payment);params['total_amount']='1.00'
        response=await client.post('/payments/notify',content=urlencode(params),headers={'content-type':'application/x-www-form-urlencoded'})
        assert response.status_code==400 and response.text=='failure'
        data=urlencode(notice(payment))+'&seller_id=duplicate'
        assert (await client.post('/payments/notify',content=data,headers={'content-type':'application/x-www-form-urlencoded'})).status_code==400
        assert (await client.post('/payments/notify',content=b'x'*65537,headers={'content-type':'application/x-www-form-urlencoded'})).status_code==400
        assert (await client.post('/payments/notify',json=notice(payment))).status_code==400
        assert (await client.get('/payments/notify')).status_code==405
        assert (await client.get('/orders')).status_code==404
        assert (await client.post('/payments/notify',content=urlencode(notice(payment)),headers={'content-type':'application/x-www-form-urlencoded'})).text=='success'


async def test_out_of_order_finished_waiting_closed_and_new_notification_id(payment):
    service, commerce, row, _ = payment
    for status in ['TRADE_FINISHED','TRADE_SUCCESS','WAIT_BUYER_PAY','TRADE_CLOSED']:
        await service.notify(notice(payment,trade_status=status))
    assert (await commerce.order(row['order_id'],1))['status']=='paid'


async def test_closed_then_late_success_requires_review_not_capture(payment):
    service, commerce, row, _ = payment
    await service.notify(notice(payment,trade_status='TRADE_CLOSED'))
    with pytest.raises(PaymentError,match='REVIEW_REQUIRED'):await service.notify(notice(payment))
    assert (await commerce.order(row['order_id'],1))['status']=='canceled'


async def test_same_notification_id_cannot_be_reused_with_different_signed_content(payment):
    service, _, _, _ = payment
    params=notice(payment)
    await service.notify(params)
    with pytest.raises(PaymentError,match='NOTIFICATION_CONFLICT'):
        await service.notify(notice(payment,notify_id=params['notify_id'],trade_status='TRADE_FINISHED'))


async def test_create_payment_reuses_merchant_number_and_checks_owner(payment):
    service, _, row, _ = payment
    a=await service.create(row['order_id'],1);b=await service.create(row['order_id'],1)
    assert parse_qs(urlsplit(a['payment_url']).query)['biz_content']==parse_qs(urlsplit(b['payment_url']).query)['biz_content']
    with pytest.raises(PaymentError,match='ORDER_NOT_FOUND'):await service.create(row['order_id'],2)


async def test_refund_ticket_owner_approval_completion_and_replay(payment):
    service, commerce, row, provider = payment
    await service.notify(notice(payment))
    support=service.support
    ticket=await support.create(SupportRequest(message='申请退货',order_id=str(row['order_id'])),uuid4().hex,1)
    actor=Principal(3,'agent',None,'fixture')
    ticket=await support.change(ticket.ticket_id,'claim',ticket.revision,actor,uuid4().hex)
    with pytest.raises(PaymentError):await service.approve_refund(ticket.ticket_id,ticket.revision,Principal(4,'agent',None,'fixture'))
    result=await service.approve_refund(ticket.ticket_id,ticket.revision,actor)
    again=await service.approve_refund(ticket.ticket_id,ticket.revision,actor)
    assert result['refund_no']==again['refund_no']
    response={'code':'10000','out_trade_no':row['merchant_no'],'trade_no':'T'+str(row['order_id']),'refund_fee':'2.00','fund_change':'Y'}
    def transport(request):return signed_response('alipay.trade.refund',response,provider)
    service.gateway=Gateway(service.config,httpx.MockTransport(transport))
    assert (await service.execute_refund(result['refund_no']))['state']=='succeeded'
    assert (await service.execute_refund(result['refund_no']))['state']=='succeeded'
    assert (await commerce.order(row['order_id'],1))['status']=='refunded'
    assert (await support.get(ticket.ticket_id)).state=='resolved'
    await service.notify(notice(payment))
    assert (await commerce.order(row['order_id'],1))['status']=='refunded'


def signed_response(method, obj, key):
    raw=json.dumps(obj,separators=(',',':'))
    sig=base64.b64encode(key.sign(raw.encode(),padding.PKCS1v15(),hashes.SHA256())).decode()
    return httpx.Response(200,content='{"'+method.replace('.','_')+'_response":'+raw+',"sign":"'+sig+'"}')


async def test_gateway_timeout_retries_bounded_and_error_does_not_leak(keys):
    config,_=keys;calls=[]
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout('DO_NOT_EXPOSE_PAYLOAD',request=request)
    with pytest.raises(PaymentError,match='^PAYMENT_GATEWAY_UNAVAILABLE$'):
        await Gateway(config,httpx.MockTransport(fail)).query('fixture')
    assert len(calls)==3
    for request in calls:assert request.extensions['timeout']['read']==.01


async def test_gateway_rejects_tampered_response_without_retry(keys):
    config,key=keys;calls=[]
    def fake(request):
        calls.append(request)
        response=signed_response('alipay.trade.query',{'code':'10000','total_amount':'1.00'},key)
        return httpx.Response(200,content=response.content.replace(b'1.00',b'9.00'))
    with pytest.raises(PaymentError,match='SIGNATURE_INVALID'):await Gateway(config,httpx.MockTransport(fake)).query('fixture')
    assert len(calls)==1


async def test_compensation_failure_stays_pending(payment):
    service,commerce,row,_=payment
    def timeout(request):raise httpx.ConnectTimeout('private diagnostic suppressed')
    service.gateway=Gateway(service.config,httpx.MockTransport(timeout))
    result=await service.compensate(row['order_id'])
    assert result['status']=='pending_confirmation'
    assert (await commerce.order(row['order_id'],1))['status']=='pending_payment'


def test_money_is_integer_cents_and_rejects_special_values():
    assert cents('1.23')==123
    for value in ['NaN','Infinity','-1','.001','invalid','1e999999999','9'*65]:
        with pytest.raises(PaymentError):cents(value)


async def test_refund_timeout_keeps_one_refund_number_and_queries_before_retry(payment):
    service, commerce, row, provider = payment
    await service.notify(notice(payment))
    support = service.support
    ticket = await support.create(SupportRequest(message='申请退货',order_id=str(row['order_id'])),uuid4().hex,1)
    actor=Principal(3,'agent',None,'fixture')
    ticket=await support.change(ticket.ticket_id,'claim',ticket.revision,actor,uuid4().hex)
    result=await service.approve_refund(ticket.ticket_id,ticket.revision,actor)
    calls=[]
    def timeout(request):
        calls.append(parse_qs(request.content.decode()))
        raise httpx.ReadTimeout('sensitive transport details')
    service.gateway=Gateway(service.config,httpx.MockTransport(timeout))
    assert (await service.execute_refund(result['refund_no']))['state']=='pending_confirmation'
    assert len(calls)==3 and len({json.loads(p['biz_content'][0])['out_request_no'] for p in calls})==1
    assert (await commerce.order(row['order_id'],1))['status']=='paid'
    query_calls=[]
    def query(request):
        params=parse_qs(request.content.decode());query_calls.append(params['method'][0])
        return signed_response('alipay.trade.fastpay.refund.query',{'code':'10000',
            'out_trade_no':row['merchant_no'],'trade_no':'T'+str(row['order_id']),
            'out_request_no':result['refund_no'],'refund_amount':'2.00','refund_status':'REFUND_SUCCESS'},provider)
    service.gateway=Gateway(service.config,httpx.MockTransport(query))
    assert (await service.execute_refund(result['refund_no']))['state']=='succeeded'
    assert query_calls==['alipay.trade.fastpay.refund.query']


async def test_expired_unsubmitted_order_releases_stock_but_ambiguous_payment_does_not(payment):
    service, commerce, row, _ = payment
    another=await commerce.place_order(PlaceOrder(user_id=1,lines=[{'product_id':801,'quantity':1}]),uuid4().hex)
    async with service.engine.begin() as c:
        await c.execute(text("UPDATE commerce.orders SET created_at=now()-interval '31 minutes' WHERE id IN (:a,:b)"),{'a':row['order_id'],'b':another['order_id']})
        await c.execute(text("UPDATE payments.payments SET created_at=now()-interval '31 minutes' WHERE order_id=:id"),{'id':row['order_id']})
    def timeout(request):raise httpx.ReadTimeout('hidden')
    service.gateway=Gateway(service.config,httpx.MockTransport(timeout))
    await service.maintain()
    assert (await commerce.order(another['order_id'],1))['status']=='canceled'
    assert (await commerce.order(row['order_id'],1))['status']=='pending_payment'


async def test_reconciliation_detects_missing_extra_wrong_amount_and_duplicate(payment):
    service, _, _row, _=payment
    await service.notify(notice(payment))
    async with service.engine.connect() as c:
        rows=(await c.execute(text("SELECT merchant_no,amount_cents FROM payments.payments WHERE state IN ('paid','refunded')"))).mappings().all()
        refunds=(await c.execute(text("SELECT refund_no,amount_cents FROM payments.refunds WHERE state='succeeded'"))).mappings().all()
    bill=[{'kind':'payment','reference':r['merchant_no'],'amount_cents':r['amount_cents']} for r in rows]
    bill += [{'kind':'refund','reference':r['refund_no'],'amount_cents':r['amount_cents']} for r in refunds]
    assert (await service.reconcile(bill))['difference_count']==0
    assert (await service.reconcile(bill[:-1]))['difference_count']>=1
    bad=[dict(r) for r in bill];bad[0]['amount_cents']+=1
    assert (await service.reconcile(bad))['difference_count']==1
    assert (await service.reconcile(bill+[bill[0]]))['difference_count']==1
    assert (await service.reconcile(bill+[{'kind':'payment','reference':'unrelated','amount_cents':1}]))['difference_count']==1


def test_private_file_permissions_raw_key_conversion_and_provider_public_key(tmp_path, keys):
    config,provider=keys
    values={'app_id.txt':'1','seller_id.txt':'2','gateway_url.txt':'https://fixture.alipaydev.com/service',
        'app_private_key.txt':base64.b64encode(config.private_key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())).decode(),
        'alipay_public_key.txt':base64.b64encode(provider.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).decode()}
    for name,value in values.items():
        p=tmp_path/name;p.write_text(value);p.chmod(0o600)
    loaded=PaymentConfig.load(tmp_path,'http://notify.invalid/payments/notify')
    assert loaded.gateway==values['gateway_url.txt']
    assert not loaded.merchant_binding_verified()
    binding = MerchantBinding.confirmed(loaded.app_id, loaded.seller_id, datetime.now(UTC).date())
    record = {'verified':True, 'verified_on':binding.verified_on.isoformat(),
              'app_digest':binding.app_digest, 'seller_digest':binding.seller_digest}
    path = tmp_path/'merchant_binding.json'
    path.write_text(json.dumps(record)); path.chmod(0o600)
    assert PaymentConfig.load(tmp_path,'http://notify.invalid/payments/notify').merchant_binding_verified()
    for change in [{'app_digest':'changed'}, {'seller_digest':'changed'},
                   {'verified_on':(datetime.now(UTC).date()+timedelta(days=2)).isoformat()}, {'verified':'true'}]:
        path.write_text(json.dumps({**record, **change}))
        with pytest.raises(PaymentError,match='CONFIGURATION_INVALID'):
            PaymentConfig.load(tmp_path,'http://notify.invalid/payments/notify')
    path.write_text(json.dumps(record)); path.chmod(0o644)
    with pytest.raises(PaymentError,match='PERMISSIONS'):
        PaymentConfig.load(tmp_path,'http://notify.invalid/payments/notify')
    path.unlink()
    signature=provider.sign(b'fixture',padding.PKCS1v15(),hashes.SHA256())
    loaded.verify(b'fixture',base64.b64encode(signature).decode())
    wrong=config.private_key.sign(b'fixture',padding.PKCS1v15(),hashes.SHA256())
    with pytest.raises(PaymentError,match='SIGNATURE_INVALID'):loaded.verify(b'fixture',base64.b64encode(wrong).decode())
    (tmp_path/'seller_id.txt').chmod(0o644)
    with pytest.raises(PaymentError,match='PERMISSIONS'):PaymentConfig.load(tmp_path,'http://notify.invalid/payments/notify')


async def test_gateway_transient_failures_retry_and_non_retryable_rejection(keys,monkeypatch):
    config,provider=keys;calls=[]
    monkeypatch.setenv('HTTPS_PROXY','http://unreachable.invalid')
    def transient(request):
        calls.append(request)
        if len(calls)<3:return httpx.Response(503)
        return signed_response('alipay.trade.query',{'code':'10000'},provider)
    assert (await Gateway(config,httpx.MockTransport(transient)).query('fixture'))['code']=='10000'
    assert len(calls)==3
    calls.clear()
    def rejection(request):
        calls.append(request)
        return signed_response('alipay.trade.query',{'code':'40004','sub_msg':'DO_NOT_EXPOSE'},provider)
    with pytest.raises(PaymentError,match='^PAYMENT_GATEWAY_REJECTED$'):await Gateway(config,httpx.MockTransport(rejection)).query('fixture')
    assert len(calls)==1


def test_bill_parser_rejects_silent_unknown_formats_and_normalizes_actual_columns():
    from agent_ops.payment_bills import normalize_bill
    rows=normalize_bill('商户订单号,业务类型,订单金额（元）\nfixture,交易,2.00\n'.encode())
    assert rows==[{'kind':'payment','reference':'fixture','amount_cents':200}]
    assert normalize_bill(b'kind,reference,amount_cents\nrefund,fixture,200\n')[0]['kind']=='refund'
    for raw in [b'unknown,column\na,b\n',b'', '商户订单号,业务类型,订单金额（元）\nfixture,退款,-2.00\n'.encode()]:
        with pytest.raises(PaymentError):normalize_bill(raw)


async def test_submitted_payment_cannot_release_reservation_locally(payment):
    from agent_ops.commerce_backend import CommerceConflict
    _service, commerce, row, _ = payment
    before = await commerce.order(row['order_id'], 1)
    with pytest.raises(CommerceConflict, match='PAYMENT_ALREADY_STARTED'):
        await commerce.cancel_order(row['order_id'], 1, before['revision'])
    after = await commerce.order(row['order_id'], 1)
    assert after['status'] == 'pending_payment'
    assert after['revision'] == before['revision']


async def test_downgrade_refuses_financial_records(payment, database):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import DBAPIError
    service, _, _, _ = payment
    await service.notify(notice(payment))
    cfg = Config('alembic.ini')
    cfg.attributes['database_url'] = database[1].url.render_as_string(hide_password=False)
    with pytest.raises(DBAPIError, match='financial records require explicit archival'):
        await asyncio.to_thread(command.downgrade, cfg, '0005_workbench')
    def revision():
        with database[1].connect() as c:
            return c.scalar(text('SELECT version_num FROM alembic_version'))
    assert await asyncio.to_thread(revision) == '0006_payments'


async def test_gateway_verifies_original_encoded_response_bytes(keys):
    config, key = keys
    response = json.dumps({'code':'10000', 'msg':'成功'}, ensure_ascii=False, separators=(',', ':')).encode('gb18030')
    signature = base64.b64encode(key.sign(response, padding.PKCS1v15(), hashes.SHA256())).decode()
    body = b'{"alipay_trade_query_response":' + response + b',"sign":"' + signature.encode() + b'"}'
    def transport(request):
        return httpx.Response(200, content=body)
    assert (await Gateway(config, httpx.MockTransport(transport)).query('fixture'))['msg'] == '成功'


@pytest.mark.parametrize('seller,reason', [
    (None, 'PAYMENT_QUERY_IDENTITY_UNAVAILABLE'),
    ('', 'PAYMENT_QUERY_IDENTITY_UNAVAILABLE'),
    ('fixture-merchant', 'PAYMENT_QUERY_IDENTITY_UNAVAILABLE'),
    ('different', 'PAYMENT_QUERY_IDENTITY_MISMATCH'),
])
async def test_signed_query_without_verified_merchant_identity_fails_closed(payment, seller, reason):
    service, commerce, row, key = payment
    calls = []
    def response(request):
        calls.append(parse_qs(request.content.decode())['method'][0])
        data = {
            'code':'10000', 'out_trade_no':row['merchant_no'],
            'trade_no':'T'+str(row['order_id']), 'trade_status':'TRADE_SUCCESS',
            'total_amount':'2.00'}
        if seller is not None:
            data['seller_id'] = seller
        return signed_response('alipay.trade.query', data, key)
    service.gateway = Gateway(service.config, httpx.MockTransport(response))
    async with service.engine.connect() as c:
        before = (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one()
    result = await service.compensate(row['order_id'])
    assert result['reason'] == reason
    assert (await commerce.order(row['order_id'], 1))['status'] == 'pending_payment'
    assert calls == ['alipay.trade.query']
    async with service.engine.connect() as c:
        assert (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one() == before
        assert await c.scalar(text("SELECT count(*) FROM payments.audit WHERE order_id=:id AND code='PAYMENT_CAPTURED'"), {'id':row['order_id']}) == 0

    # A blocked query must not poison later authenticated notification processing.
    params = notice(payment)
    await service.notify(params)
    await service.notify(params)
    assert (await commerce.order(row['order_id'], 1))['status'] == 'paid'
    async with service.engine.connect() as c:
        assert await c.scalar(text("SELECT count(*) FROM payments.audit WHERE order_id=:id AND code='PAYMENT_CAPTURED'"), {'id':row['order_id']}) == 1


async def test_manual_binding_compensation_and_repeated_queries_capture_once(payment):
    service, commerce, row, key = payment
    original = service.config
    service.config = replace(original, merchant_binding=MerchantBinding.confirmed(
        original.app_id, original.seller_id, datetime.now(UTC).date()))
    def response(request):
        return signed_response('alipay.trade.query', {
            'code':'10000', 'out_trade_no':row['merchant_no'],
            'trade_no':'T'+str(row['order_id']), 'trade_status':'TRADE_SUCCESS',
            'total_amount':'2.00'}, key)
    service.gateway = Gateway(service.config, httpx.MockTransport(response))
    async with service.engine.connect() as c:
        before = (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one()
    for _ in range(5):
        assert (await service.compensate(row['order_id']))['status'] == 'confirmed'
    await asyncio.gather(service.compensate(row['order_id']), service.compensate(row['order_id']))
    assert (await commerce.order(row['order_id'], 1))['status'] == 'paid'
    async with service.engine.connect() as c:
        after = (await c.execute(text('SELECT stock,reserved FROM commerce.inventory WHERE product_id=801'))).one()
        assert before.stock-after.stock == 2 and before.reserved-after.reserved == 2
        for code in ['PAYMENT_CAPTURED', 'PAYMENT_CAPTURED_MANUAL_BINDING']:
            assert await c.scalar(text('SELECT count(*) FROM payments.audit WHERE order_id=:id AND code=:code'), {'id':row['order_id'], 'code':code}) == 1
    assert not replace(service.config, app_id='changed').merchant_binding_verified()
    assert not replace(service.config, seller_id='changed').merchant_binding_verified()


@pytest.mark.parametrize('change,reason', [
    ({'total_amount':'2.01'}, 'PAYMENT_AMOUNT_MISMATCH'),
    ({'out_trade_no':'different'}, 'PAYMENT_QUERY_IDENTITY_MISMATCH'),
    ({'seller_id':'different'}, 'PAYMENT_QUERY_IDENTITY_MISMATCH'),
    ({'trade_status':'WAIT_BUYER_PAY'}, 'PAYMENT_QUERY_STATUS_UNCONFIRMED'),
    ({'trade_status':'TRADE_CLOSED'}, 'PAYMENT_QUERY_STATUS_UNCONFIRMED'),
])
async def test_manual_binding_does_not_bypass_query_checks(payment, change, reason):
    service, commerce, row, key = payment
    original = service.config
    service.config = replace(original, merchant_binding=MerchantBinding.confirmed(
        original.app_id, original.seller_id, datetime.now(UTC).date()))
    def response(request):
        return signed_response('alipay.trade.query', {
            'code':'10000', 'out_trade_no':row['merchant_no'],
            'trade_no':'T'+str(row['order_id']), 'trade_status':'TRADE_SUCCESS',
            'total_amount':'2.00', **change}, key)
    service.gateway = Gateway(service.config, httpx.MockTransport(response))
    assert (await service.compensate(row['order_id']))['reason'] == reason
    assert (await commerce.order(row['order_id'], 1))['status'] == 'pending_payment'


@pytest.mark.parametrize('seller,reason', [
    (None, 'PAYMENT_MERCHANT_UNAVAILABLE'),
    ('', 'PAYMENT_MERCHANT_UNAVAILABLE'), ('different', 'PAYMENT_MERCHANT_MISMATCH'),
])
async def test_manual_binding_never_relaxes_notification_merchant_check(payment, seller, reason):
    service, commerce, row, _ = payment
    service.config = replace(service.config, merchant_binding=MerchantBinding.confirmed(
        service.config.app_id, service.config.seller_id, datetime.now(UTC).date()))
    params = notice(payment, seller_id=seller or '')
    if seller is None:
        params.pop('seller_id')
    with pytest.raises(PaymentError, match=reason):
        await service.notify(params)
    assert (await commerce.order(row['order_id'], 1))['status'] == 'pending_payment'


async def test_manual_binding_never_bypasses_provider_signature(payment):
    service, commerce, row, key = payment
    original = service.config
    service.config = replace(original, merchant_binding=MerchantBinding.confirmed(
        original.app_id, original.seller_id, datetime.now(UTC).date()))
    def response(request):
        signed = signed_response('alipay.trade.query', {
            'code':'10000', 'out_trade_no':row['merchant_no'],
            'trade_no':'T'+str(row['order_id']), 'trade_status':'TRADE_SUCCESS',
            'total_amount':'2.00'}, key)
        return httpx.Response(200, content=signed.content.replace(b'2.00', b'3.00'))
    service.gateway = Gateway(service.config, httpx.MockTransport(response))
    assert (await service.compensate(row['order_id']))['reason'] == 'PAYMENT_SIGNATURE_INVALID'
    assert (await commerce.order(row['order_id'], 1))['status'] == 'pending_payment'
