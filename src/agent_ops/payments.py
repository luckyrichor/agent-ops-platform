"""P6 PostgreSQL ledger. Financial transitions share the order/inventory transaction.

No raw gateway payloads, credentials, network addresses or signed URLs are persisted in audit.
"""
import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from agent_ops.auth import Principal
from agent_ops.payment_gateway import Gateway, PaymentConfig, PaymentError, canonical, cents
from agent_ops.workbench import WorkbenchService


class Payments:
    def __init__(self, url: str, config: PaymentConfig, gateway: Gateway | None = None,
                 support: WorkbenchService | None = None) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.config, self.gateway, self.support = config, gateway or Gateway(config), support

    async def aclose(self) -> None:
        await self.engine.dispose()

    @staticmethod
    async def audit(c: AsyncConnection, code: str, order: int | None = None,
                    actor: str = 'system') -> None:
        await c.execute(text('INSERT INTO payments.audit(order_id,code,actor) VALUES(:id,:code,:actor)'),
                        {'id': order, 'code': code, 'actor': actor})

    async def reject(self, code: str) -> None:
        async with self.engine.begin() as c:
            await self.audit(c, code)

    async def create(self, order_id: int, user_id: int) -> dict[str, Any]:
        async with self.engine.begin() as c:
            order = (await c.execute(text('SELECT * FROM commerce.orders WHERE id=:id AND user_id=:user FOR UPDATE'), {'id': order_id, 'user': user_id})).mappings().first()
            if order is None:
                raise PaymentError('ORDER_NOT_FOUND')
            if order['status'] != 'pending_payment' or order['total_cents'] <= 0:
                raise PaymentError('PAYMENT_ORDER_STATE')
            if (datetime.now(UTC) - order['created_at']).total_seconds() >= 1800:
                raise PaymentError('PAYMENT_ORDER_EXPIRED')
            row = (await c.execute(text('SELECT * FROM payments.payments WHERE order_id=:id'), {'id': order_id})).mappings().first()
            if row is None:
                merchant = 'P' + uuid4().hex
                await c.execute(text('INSERT INTO payments.payments(order_id,merchant_no,amount_cents) VALUES(:id,:no,:amount)'), {'id': order_id, 'no': merchant, 'amount': order['total_cents']})
                await self.audit(c, 'PAYMENT_CREATED', order_id, 'customer')
            else:
                if row['state'] != 'pending' or row['amount_cents'] != order['total_cents']:
                    raise PaymentError('PAYMENT_ORDER_STATE')
                merchant = row['merchant_no']
            return {'order_id': order_id, 'payment_url': self.config.payment_url(merchant, order['total_cents']),
                    'state': 'pending'}

    @staticmethod
    async def inventory(c: AsyncConnection, order_id: int, *, capture: bool) -> None:
        items = (await c.execute(text('SELECT product_id,quantity FROM commerce.order_items WHERE order_id=:id ORDER BY product_id'), {'id': order_id})).mappings()
        for item in items:
            result = await c.scalar(text('''UPDATE commerce.inventory SET reserved=reserved-:q,
                stock=stock-:deduct,version=version+1 WHERE product_id=:id AND reserved>=:q
                AND stock>=:deduct RETURNING product_id'''),
                {'id': item['product_id'], 'q': item['quantity'], 'deduct': item['quantity'] if capture else 0})
            if result is None:
                raise PaymentError('PAYMENT_INVENTORY_CONFLICT')

    async def notify(self, params: dict[str, str]) -> None:
        try:
            self.config.notification(params)
            await self.apply(params, notification=True)
        except PaymentError as error:
            await self.reject(str(error))
            raise

    async def apply(self, params: dict[str, str], *, notification: bool,
                    manual_binding: bool = False) -> None:
        merchant = params.get('out_trade_no', '')
        status = params.get('trade_status', '')
        if status not in {'WAIT_BUYER_PAY', 'TRADE_SUCCESS', 'TRADE_FINISHED', 'TRADE_CLOSED'}:
            raise PaymentError('PAYMENT_STATUS_INVALID')
        trade = params.get('trade_no', '')
        if not trade or len(trade) > 128 or not merchant or len(merchant) > 128:
            raise PaymentError('PAYMENT_TRADE_INVALID')
        amount = cents(params.get('total_amount', ''))
        async with self.engine.begin() as c:
            # Lock order first on every path, including cancellation, refund and expiry.
            order = (await c.execute(text('''SELECT o.* FROM commerce.orders o JOIN payments.payments p
                 ON p.order_id=o.id WHERE p.merchant_no=:no FOR UPDATE OF o'''), {'no': merchant})).mappings().first()
            if order is None:
                raise PaymentError('PAYMENT_ORDER_UNKNOWN')
            payment = (await c.execute(text('SELECT * FROM payments.payments WHERE merchant_no=:no FOR UPDATE'), {'no': merchant})).mappings().one()
            oid = order['id']
            if amount != order['total_cents'] or amount != payment['amount_cents']:
                raise PaymentError('PAYMENT_AMOUNT_MISMATCH')
            if payment['provider_trade'] and payment['provider_trade'] != trade:
                raise PaymentError('PAYMENT_TRADE_MISMATCH')
            if notification:
                nid = params.get('notify_id', '')
                if not nid or len(nid) > 128:
                    raise PaymentError('PAYMENT_NOTIFICATION_INVALID')
                digest = hashlib.sha256(canonical(params, notification=True)).hexdigest()
                existing = (await c.execute(text('''INSERT INTO payments.notifications(notification_id,payload_hash,merchant_no)
                  VALUES(:id,:hash,:no) ON CONFLICT DO NOTHING RETURNING notification_id'''), {'id': nid, 'hash': digest, 'no': merchant})).first()
                if existing is None:
                    previous: str = (await c.execute(text('SELECT payload_hash FROM payments.notifications WHERE notification_id=:id'), {'id': nid})).scalar_one()
                    if previous != digest:
                        raise PaymentError('PAYMENT_NOTIFICATION_CONFLICT')
                    return
            if status in {'TRADE_SUCCESS', 'TRADE_FINISHED'}:
                if payment['state'] == 'pending':
                    if order['status'] != 'pending_payment':
                        raise PaymentError('PAYMENT_LATE_SUCCESS_REVIEW_REQUIRED')
                    await self.inventory(c, oid, capture=True)
                    await c.execute(text("UPDATE commerce.orders SET status='paid',revision=revision+1 WHERE id=:id"), {'id': oid})
                    await c.execute(text("UPDATE payments.payments SET state='paid',provider_trade=:trade,paid_at=now() WHERE order_id=:id"), {'id': oid, 'trade': trade})
                    await self.audit(c, 'PAYMENT_CAPTURED', oid)
                    if manual_binding:
                        await self.audit(c, 'PAYMENT_CAPTURED_MANUAL_BINDING', oid)
                elif payment['state'] == 'closed':
                    raise PaymentError('PAYMENT_LATE_SUCCESS_REVIEW_REQUIRED')
                # Repeated success after refund/finish never regresses the ledger or inventory.
            elif status == 'TRADE_CLOSED' and payment['state'] == 'pending':
                if order['status'] == 'pending_payment':
                    await self.inventory(c, oid, capture=False)
                    await c.execute(text("UPDATE commerce.orders SET status='canceled',revision=revision+1 WHERE id=:id"), {'id': oid})
                await c.execute(text("UPDATE payments.payments SET state='closed',provider_trade=:trade WHERE order_id=:id"), {'id': oid, 'trade': trade})
                await self.audit(c, 'PAYMENT_CLOSED', oid)
            else:
                await self.audit(c, 'PAYMENT_STALE_OR_WAITING', oid)

    async def compensate(self, order_id: int) -> dict[str, Any]:
        async with self.engine.connect() as c:
            row = (await c.execute(text('SELECT * FROM payments.payments WHERE order_id=:id'), {'id': order_id})).mappings().first()
        if row is None:
            raise PaymentError('PAYMENT_ORDER_UNKNOWN')
        # Completed refunds are authoritative. A payment-query rejection after
        # refund is not an ambiguous payment and must not create failure audits.
        async with self.engine.connect() as c:
            refunded = await c.scalar(text("SELECT 1 FROM payments.refunds WHERE order_id=:id AND state='succeeded'"), {'id': order_id})
        if row['state'] == 'refunded' or refunded:
            return {'order_id': order_id, 'status': 'refunded'}
        try:
            data = await self.gateway.query(row['merchant_no'])
            if data.get('out_trade_no') != row['merchant_no']:
                raise PaymentError('PAYMENT_QUERY_IDENTITY_MISMATCH')
            if data.get('seller_id') and data['seller_id'] != self.config.seller_id:
                raise PaymentError('PAYMENT_QUERY_IDENTITY_MISMATCH')
            if not self.config.merchant_binding_verified():
                raise PaymentError('PAYMENT_QUERY_IDENTITY_UNAVAILABLE')
            if not data.get('seller_id') and data.get('trade_status') not in {'TRADE_SUCCESS', 'TRADE_FINISHED'}:
                raise PaymentError('PAYMENT_QUERY_STATUS_UNCONFIRMED')
            if data.get('trade_status') == 'WAIT_BUYER_PAY' and (datetime.now(UTC) - row['created_at']).total_seconds() >= 1800:
                closed = await self.gateway.call('alipay.trade.close', {'out_trade_no': row['merchant_no']})
                if closed.get('out_trade_no') != row['merchant_no'] or closed.get('trade_no') != data.get('trade_no'):
                    raise PaymentError('PAYMENT_QUERY_IDENTITY_MISMATCH')
                data['trade_status'] = 'TRADE_CLOSED'
            await self.apply({k: str(v) for k, v in data.items()}, notification=False,
                             manual_binding=True)
            return {'order_id': order_id, 'status': 'confirmed'}
        except PaymentError as error:
            # A refund may have completed while the external query was in flight.
            # Serialize with finish_refund before declaring a payment failure.
            async with self.engine.begin() as c:
                state = await c.scalar(text('SELECT status FROM commerce.orders WHERE id=:id FOR UPDATE'), {'id': order_id})
                if state == 'refunded':
                    return {'order_id': order_id, 'status': 'refunded'}
                await self.audit(c, str(error))
            return {'order_id': order_id, 'status': 'pending_confirmation', 'reason': str(error)}

    async def approve_refund(self, ticket_id: str, revision: int, principal: Principal, *,
                             requested_amount_cents: int | None = None) -> dict[str, Any]:
        if self.support is None or principal.role not in {'agent', 'admin'}:
            raise PaymentError('REFUND_FORBIDDEN')
        async with self.engine.begin() as c:
            oid = await c.scalar(text('SELECT order_id FROM support.tickets WHERE ticket_id=:id'), {'id': ticket_id})
            if oid is None:
                raise PaymentError('REFUND_TICKET_INVALID')
            order = (await c.execute(text('SELECT * FROM commerce.orders WHERE id=:id FOR UPDATE'), {'id': oid})).mappings().one()
            payment = (await c.execute(text('SELECT * FROM payments.payments WHERE order_id=:id FOR UPDATE'), {'id': oid})).mappings().first()
            ticket = await self.support._row(c, ticket_id, lock=True)
            if requested_amount_cents is not None and requested_amount_cents > order['total_cents']:
                raise PaymentError('REFUND_AMOUNT_EXCEEDS_PAID')
            if requested_amount_cents is not None and requested_amount_cents != order['total_cents']:
                raise PaymentError('PARTIAL_REFUND_UNSUPPORTED')
            existing = (await c.execute(text('SELECT * FROM payments.refunds WHERE order_id=:id'), {'id': oid})).mappings().first()
            if existing:
                if str(existing['ticket_id']) != ticket_id or existing['approved_by'] != principal.account_id:
                    raise PaymentError('REFUND_FORBIDDEN')
                return {'refund_no': existing['refund_no'], 'state': existing['state']}
            now: datetime = (await c.execute(text('SELECT now()'))).scalar_one()
            if ticket.route != 'return_request' or ticket.state != 'claimed' or ticket.assigned_account_id != principal.account_id or ticket.revision != revision or ticket.lease_expires_at is None or ticket.lease_expires_at <= now or ticket.customer_user_id != order['user_id']:
                raise PaymentError('REFUND_TICKET_NOT_CLAIMED')
            if payment is None or payment['state'] != 'paid' or order['status'] != 'paid' or payment['amount_cents'] != order['total_cents']:
                raise PaymentError('REFUND_ORDER_STATE')
            rid = 'R' + uuid4().hex
            await c.execute(text('''INSERT INTO payments.refunds(refund_no,order_id,ticket_id,amount_cents,state,approved_by)
              VALUES(:no,:oid,:tid,:amount,'pending',:actor)'''), {'no': rid, 'oid': oid, 'tid': ticket_id, 'amount': order['total_cents'], 'actor': principal.account_id})
            ticket.revision += 1
            await c.execute(text("UPDATE support.tickets SET revision=:r,updated_at=now(),lease_expires_at=now()+interval '10 minutes' WHERE ticket_id=:id"), {'r': ticket.revision, 'id': ticket_id})
            await self.support._event(c, ticket, 'refund_approved', principal.actor)
            await self.audit(c, 'REFUND_APPROVED', oid, principal.actor)
            return {'refund_no': rid, 'state': 'pending'}

    async def execute_refund(self, refund_no: str) -> dict[str, Any]:
        async with self.engine.connect() as c:
            refund = (await c.execute(text('''SELECT r.*,p.merchant_no,p.provider_trade FROM payments.refunds r
              JOIN payments.payments p USING(order_id) WHERE refund_no=:no'''), {'no': refund_no})).mappings().first()
        if refund is None:
            raise PaymentError('REFUND_NOT_FOUND')
        if refund['state'] == 'succeeded':
            return {'refund_no': refund_no, 'state': 'succeeded'}
        try:
            if refund['state'] == 'unknown':
                response = await self.gateway.refund_query(refund['merchant_no'], refund_no)
                confirmed = response.get('refund_status') == 'REFUND_SUCCESS'
                amount = response.get('refund_amount', '')
            else:
                response = await self.gateway.refund(refund['merchant_no'], refund_no, refund['amount_cents'])
                confirmed = response.get('fund_change') in {'Y', 'N'}
                amount = response.get('refund_fee', '')
            if not confirmed or cents(str(amount)) != refund['amount_cents'] or response.get('out_trade_no') != refund['merchant_no'] or response.get('trade_no') != refund['provider_trade']:
                raise PaymentError('REFUND_RESULT_UNCONFIRMED')
            if refund['state'] == 'unknown' and response.get('out_request_no') != refund_no:
                raise PaymentError('REFUND_RESULT_UNCONFIRMED')
            await self.finish_refund(refund_no)
            return {'refund_no': refund_no, 'state': 'succeeded'}
        except PaymentError as error:
            async with self.engine.begin() as c:
                await c.execute(text("UPDATE payments.refunds SET state='unknown' WHERE refund_no=:no AND state!='succeeded'"), {'no': refund_no})
                await self.audit(c, str(error), refund['order_id'])
            return {'refund_no': refund_no, 'state': 'pending_confirmation', 'reason': str(error)}

    async def finish_refund(self, refund_no: str) -> None:
        async with self.engine.begin() as c:
            oid = await c.scalar(text('SELECT order_id FROM payments.refunds WHERE refund_no=:no'), {'no': refund_no})
            await c.execute(text('SELECT id FROM commerce.orders WHERE id=:id FOR UPDATE'), {'id': oid})
            await c.execute(text('SELECT order_id FROM payments.payments WHERE order_id=:id FOR UPDATE'), {'id': oid})
            refund = (await c.execute(text('SELECT * FROM payments.refunds WHERE refund_no=:no FOR UPDATE'), {'no': refund_no})).mappings().one()
            if refund['state'] == 'succeeded':
                return
            await c.execute(text("UPDATE payments.refunds SET state='succeeded',succeeded_at=now() WHERE refund_no=:no"), {'no': refund_no})
            await c.execute(text("UPDATE payments.payments SET state='refunded' WHERE order_id=:id"), {'id': oid})
            await c.execute(text("UPDATE commerce.orders SET status='refunded',revision=revision+1 WHERE id=:id"), {'id': oid})
            await self.audit(c, 'REFUND_SUCCEEDED', oid)
            if self.support:
                ticket = await self.support._row(c, str(refund['ticket_id']), lock=True)
                ticket.state, ticket.revision = 'resolved', ticket.revision + 1
                ticket.assigned_account_id, ticket.lease_expires_at = None, None
                ticket.reason_code = 'REFUND_SUCCEEDED'
                ticket.order_status = 'refunded'
                await c.execute(text("""UPDATE support.tickets SET state='resolved',revision=:r,
                    assigned_account_id=NULL,lease_expires_at=NULL,reason_code='REFUND_SUCCEEDED',order_status='refunded',updated_at=now()
                    WHERE ticket_id=:id"""), {'r': ticket.revision, 'id': ticket.ticket_id})
                await self.support._message(c, ticket, 'system', 'system', 'REFUND_SUCCEEDED')
                await self.support._event(c, ticket, 'refund_succeeded', 'system')

    async def maintain(self) -> None:
        # Query ambiguous payment/refund outcomes before releasing any reservation.
        async with self.engine.connect() as c:
            pending: Sequence[int] = (await c.execute(text("""SELECT order_id FROM payments.payments
                WHERE state='pending' AND next_query_at<=now() ORDER BY order_id LIMIT 20"""))).scalars().all()
            refunds: Sequence[str] = (await c.execute(text("SELECT refund_no FROM payments.refunds WHERE state IN ('pending','unknown') AND next_query_at<=now() ORDER BY created_at LIMIT 20"))).scalars().all()
        for oid in pending:
            async with self.engine.begin() as c:
                won = await c.scalar(text("UPDATE payments.payments SET next_query_at=now()+interval '2 minutes' WHERE order_id=:id AND next_query_at<=now() RETURNING order_id"), {'id': oid})
            if won is not None:
                await self.compensate(oid)
        for rid in refunds:
            async with self.engine.begin() as c:
                won = await c.scalar(text("UPDATE payments.refunds SET next_query_at=now()+interval '2 minutes' WHERE refund_no=:no AND next_query_at<=now() RETURNING refund_no"), {'no': rid})
            if won is not None:
                await self.execute_refund(rid)
        # Only never-submitted orders may be canceled locally; submitted orders require provider closure.
        async with self.engine.begin() as c:
            orders: Sequence[int] = (await c.execute(text("""SELECT o.id FROM commerce.orders o WHERE status='pending_payment'
                AND created_at<now()-interval '30 minutes' AND NOT EXISTS
                (SELECT 1 FROM payments.payments p WHERE p.order_id=o.id) ORDER BY o.id LIMIT 20 FOR UPDATE OF o"""))).scalars().all()
            for oid in orders:
                await self.inventory(c, oid, capture=False)
                await c.execute(text("UPDATE commerce.orders SET status='canceled',revision=revision+1 WHERE id=:id"), {'id': oid})
                await self.audit(c, 'ORDER_EXPIRED', oid)

    async def reconcile(self, records: list[dict[str, Any]], bill_date: date | None = None) -> dict[str, Any]:
        """Compare externally supplied bill rows with both ledgers, including missing/extra rows."""
        async with self.engine.connect() as c:
            local = (await c.execute(text('''SELECT p.merchant_no,p.amount_cents,p.state,p.paid_at,r.succeeded_at,r.refund_no,
                r.amount_cents AS refund_amount,r.state AS refund_state FROM payments.payments p
                LEFT JOIN payments.refunds r USING(order_id)'''))).mappings().all()
        expected: dict[tuple[str, str], int] = {}
        for row in local:
            if row['state'] in {'paid', 'refunded'} and (bill_date is None or row['paid_at'].astimezone(timezone(timedelta(hours=8))).date() == bill_date):
                expected[('payment', row['merchant_no'])] = row['amount_cents']
            if row['refund_state'] == 'succeeded' and (bill_date is None or row['succeeded_at'].astimezone(timezone(timedelta(hours=8))).date() == bill_date):
                expected[('refund', row['refund_no'])] = row['refund_amount']
        actual: dict[tuple[str, str], int] = {}
        differences = []
        for bill in records:
            key = (str(bill['kind']), str(bill['reference']))
            if key in actual:
                differences.append({'kind': key[0], 'reference': key[1], 'reason': 'duplicate_bill_row'})
            actual[key] = int(bill['amount_cents'])
        for key in sorted(expected.keys() | actual.keys()):
            if expected.get(key) != actual.get(key):
                differences.append({'kind': key[0], 'reference': key[1], 'reason': 'amount_or_presence_mismatch'})
        return {'differences': differences, 'difference_count': len(differences)}
