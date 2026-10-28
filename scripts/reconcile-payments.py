"""Read-only reconciliation against an external bill, with no credential/payload output."""
import argparse
import asyncio
import json
import os
from datetime import date
from pathlib import Path

from agent_ops.payment_api import from_env
from agent_ops.payment_bills import download_bill, normalize_bill
from agent_ops.payment_gateway import PaymentError
from agent_ops.payments import Payments

p = argparse.ArgumentParser()
p.add_argument('--env-file', type=Path, required=True)
p.add_argument('--payment-dir', type=Path, default=Path('.local/alipay'))
p.add_argument('--bill', type=Path)
p.add_argument('--bill-date', type=date.fromisoformat)
p.add_argument('--download-date', type=date.fromisoformat)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
if a.bill and not a.bill_date:
    p.error('external bill requires bill-date to bound the local ledger')
if bool(a.bill) == bool(a.download_date):
    p.error('choose one external bill file or download date')
for path in (a.env_file, a.payment_dir, a.output, *([a.bill] if a.bill else [])):
    if '.local' not in path.resolve().parts:
        p.error('inputs/output must remain in ignored .local')
os.umask(0o077)
os.environ.update({k: v for k, v in (line.split('=', 1) for line in a.env_file.read_text().splitlines() if line and not line.startswith('#')) if k.startswith('AGENT_OPS_')})
os.environ['AGENT_OPS_PAYMENT_DIR'] = str(a.payment_dir.resolve())


async def main():
    service = None
    try:
        url = os.environ['AGENT_OPS_DATABASE_URL']
        config = from_env(url)
        if config is None:
            raise PaymentError('PAYMENTS_NOT_CONFIGURED')
        service = Payments(url, config)
        content = a.bill.read_bytes() if a.bill else await download_bill(service.gateway, str(a.download_date))
        result = await service.reconcile(normalize_bill(content), a.download_date or a.bill_date)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        a.output.chmod(0o600)
        print('Reconciliation differences:', result['difference_count'])
        return 0 if result['difference_count'] == 0 else 1
    except (PaymentError, OSError, ValueError, KeyError):
        print('Reconciliation unavailable; private details suppressed; not a zero-difference result.')
        return 2
    finally:
        if service:
            await service.aclose()


raise SystemExit(asyncio.run(main()))
