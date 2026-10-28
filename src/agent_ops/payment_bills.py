"""Restricted bill retrieval and explicit, fail-closed normalization. No raw bills in logs."""
import csv
import io
import zipfile
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_ops.payment_gateway import Gateway, PaymentError, cents


def normalize_bill(data: bytes) -> list[dict[str, Any]]:
    if len(data) > 10 * 1024 * 1024:
        raise PaymentError('BILL_TOO_LARGE')
    chunks: list[bytes] = []
    if data.startswith(b'PK'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                files = [f for f in archive.infolist() if f.filename.endswith('.csv') and '业务明细' in f.filename]
                if not files or sum(f.file_size for f in files) > 10 * 1024 * 1024:
                    raise PaymentError('BILL_FORMAT_UNSUPPORTED')
                chunks = [archive.read(f) for f in files]
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise PaymentError('BILL_FORMAT_UNSUPPORTED') from None
    else:
        chunks = [data]
    records = []
    for chunk in chunks:
        try:
            raw = chunk.decode('utf-8-sig')
        except UnicodeError:
            raw = chunk.decode('gb18030')
        lines = [line for line in raw.splitlines() if line.strip() and not line.startswith('#')]
        if not lines:
            raise PaymentError('BILL_FORMAT_UNSUPPORTED')
        reader = csv.DictReader(lines)
        headers = {h.strip() for h in (reader.fieldnames or [])}
        normalized = {'kind', 'reference', 'amount_cents'} <= headers
        official = {'商户订单号', '业务类型', '订单金额（元）'} <= headers
        if not normalized and not official:
            raise PaymentError('BILL_FORMAT_UNSUPPORTED')
        for source in reader:
            row = {str(k).strip(): str(v).strip() for k, v in source.items()}
            if normalized:
                kind, reference = row['kind'], row['reference']
                try:
                    amount = int(row['amount_cents'])
                except ValueError:
                    raise PaymentError('BILL_AMOUNT_INVALID') from None
            elif row['业务类型'] == '交易':
                kind, reference, amount = 'payment', row['商户订单号'], cents(row['订单金额（元）'])
            elif row['业务类型'] == '退款':
                # Fail closed when the bill cannot identify our unique refund request.
                reference = row.get('商户退款请求号', '')
                if not reference:
                    raise PaymentError('BILL_REFUND_REFERENCE_MISSING')
                kind, amount = 'refund', cents(row['订单金额（元）'].lstrip('-'))
            else:
                raise PaymentError('BILL_BUSINESS_TYPE_UNSUPPORTED')
            if kind not in {'payment', 'refund'} or not reference or len(reference) > 128 or amount < 0:
                raise PaymentError('BILL_ROW_INVALID')
            records.append({'kind': kind, 'reference': reference, 'amount_cents': amount})
    return records


async def download_bill(gateway: Gateway, date: str) -> bytes:
    result = await gateway.call('alipay.data.dataservice.bill.downloadurl.query',
                                {'bill_type': 'trade', 'bill_date': date})
    url = result.get('bill_download_url', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(('.alipay.com', '.alipaydev.com')) or parsed.username or parsed.fragment:
        raise PaymentError('BILL_DOWNLOAD_HOST_REJECTED')
    for attempt in range(gateway.config.retries + 1):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=gateway.config.timeout,
                                         follow_redirects=False) as client, client.stream('GET', url) as response:
                if response.status_code != 200:
                    raise PaymentError('BILL_DOWNLOAD_UNAVAILABLE')
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > 10 * 1024 * 1024:
                        raise PaymentError('BILL_TOO_LARGE')
                return bytes(content)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == gateway.config.retries:
                break
    raise PaymentError('BILL_DOWNLOAD_UNAVAILABLE')
