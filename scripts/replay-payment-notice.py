"""Replay private, encrypted notification evidence against the isolated local entry."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import text

from agent_ops.payment_api import from_env
from agent_ops.payment_gateway import PaymentError
from agent_ops.payment_notices import NoticeArchive
from agent_ops.payments import Payments

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--env-file', type=Path, required=True)
p.add_argument('--notice', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--count', type=int, default=5)
a = p.parse_args()
root = (Path.cwd()/'.local').resolve()
if not 5 <= a.count <= 20 or any(not path.resolve().is_relative_to(root) for path in (a.env_file, a.notice, a.output)):
    p.error('private paths and 5..20 replays required')
os.umask(0o077)


async def main():
    service = None
    try:
        os.environ.update({k:v for k,v in (line.split('=',1) for line in a.env_file.read_text().splitlines()
            if line and not line.startswith('#')) if k.startswith('AGENT_OPS_')})
        params = NoticeArchive(a.notice.parent).load(a.notice)
        config = from_env('')
        if config is None:
            raise ValueError
        config.notification(params)
        service = Payments(os.environ['AGENT_OPS_DATABASE_URL'], config)

        async def capture_count():
            async with service.engine.connect() as c:
                return await c.scalar(text("""SELECT count(*) FROM payments.audit a JOIN payments.payments p
                    ON p.order_id=a.order_id WHERE p.merchant_no=:no AND a.code='PAYMENT_CAPTURED'"""),
                    {'no':params['out_trade_no']})

        before = await capture_count()
        async with service.engine.connect() as c:
            invalid_before = await c.scalar(text("SELECT count(*) FROM payments.audit WHERE code='PAYMENT_SIGNATURE_INVALID'"))
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            statuses = []
            for _ in range(a.count):
                response = await client.post('http://127.0.0.1:18080/payments/notify', data=params)
                statuses.append(response.status_code)
                if response.status_code != 200 or response.text != 'success':
                    raise ValueError
            changed = dict(params)
            changed['total_amount'] = '99999999.99' if params['total_amount'] != '99999999.99' else '0.01'
            try:
                config.notification(changed)
                raise ValueError
            except PaymentError as error:
                if str(error) != 'PAYMENT_SIGNATURE_INVALID':
                    raise ValueError from None
            tampered = await client.post('http://127.0.0.1:18080/payments/notify', data=changed)
        after = await capture_count()
        async with service.engine.connect() as c:
            invalid_after = await c.scalar(text("SELECT count(*) FROM payments.audit WHERE code='PAYMENT_SIGNATURE_INVALID'"))
        result = {'replay_count':a.count, 'replay_http_statuses':statuses,
                  'capture_events_before':before, 'capture_events_after':after,
                  'tampered_http_status':tampered.status_code,
                  'signature_invalid_audit_increment':invalid_after-invalid_before,
                  'passed':before == after == 1 and tampered.status_code == 400 and invalid_after > invalid_before}
        a.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        a.output.write_text(json.dumps(result, indent=2)+'\n')
        a.output.chmod(0o600)
        print(json.dumps(result))
        return 0 if result['passed'] else 1
    except Exception:  # noqa: BLE001 -- private diagnostic values must never escape
        print('Notification replay verification incomplete; private details suppressed.')
        return 2
    finally:
        if service:
            await service.aclose()


raise SystemExit(asyncio.run(main()))
