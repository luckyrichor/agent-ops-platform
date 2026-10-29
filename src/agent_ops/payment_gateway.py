"""Private-file configuration and bounded, certificate-verified sandbox transport.

Never log provider payloads, URLs, signatures or exception text. A timeout means unknown.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Library request logs expose the configured endpoint. Provider diagnostics are fixed codes only.
logging.getLogger("httpx").disabled = True
logging.getLogger("httpcore").setLevel(logging.CRITICAL)


class PaymentError(RuntimeError):
    """Only fixed, non-sensitive reason codes cross the application boundary."""


def cents(value: str) -> int:
    try:
        if len(value) > 64:
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > Decimal(9223372036854775807) / 100 or number * 100 != (number * 100).to_integral_value():
            raise ValueError
        return int(number * 100)
    except (InvalidOperation, ValueError, OverflowError):
        raise PaymentError('PAYMENT_AMOUNT_INVALID') from None


def money(value: int) -> str:
    return f'{value // 100}.{value % 100:02d}'


def canonical(params: dict[str, str], *, notification: bool = False) -> bytes:
    excluded = {'sign', 'sign_type'} if notification else {'sign'}
    return '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if k not in excluded and v != '').encode()


@dataclass(frozen=True, repr=False)
class MerchantBinding:
    """Operator attestation, never a provider-signed proof of merchant identity."""

    verified_on: date
    app_digest: str
    seller_digest: str

    @classmethod
    def confirmed(cls, app: str, seller: str, verified_on: date) -> 'MerchantBinding':
        return cls(verified_on, hashlib.sha256(app.encode()).hexdigest(),
                   hashlib.sha256(seller.encode()).hexdigest())

    def matches(self, app: str, seller: str) -> bool:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        return self == self.confirmed(app, seller, self.verified_on) and self.verified_on <= today


@dataclass(repr=False)
class PaymentConfig:
    app_id: str
    seller_id: str
    gateway: str
    notify_url: str
    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey
    timeout: float = 5
    retries: int = 2
    merchant_binding: MerchantBinding | None = None

    def merchant_binding_verified(self) -> bool:
        return self.merchant_binding is not None and self.merchant_binding.matches(self.app_id, self.seller_id)

    @classmethod
    def load(cls, folder: Path, notify_url: str) -> 'PaymentConfig':
        def read(name: str) -> str:
            p = folder / name
            if p.is_symlink() or stat.S_IMODE(p.stat().st_mode) != 0o600:
                raise PaymentError('PAYMENT_FILE_PERMISSIONS')
            return p.read_text().strip().lstrip('\ufeff')
        try:
            app, seller, gateway = read('app_id.txt'), read('seller_id.txt'), read('gateway_url.txt')
            private, public = read('app_private_key.txt'), read('alipay_public_key.txt')
            endpoint, notify = urlsplit(gateway), urlsplit(notify_url)
            if not app.isdigit() or not seller.isdigit():
                raise ValueError
            if endpoint.scheme != 'https' or not endpoint.hostname or not endpoint.hostname.endswith('.alipaydev.com') or endpoint.query or endpoint.fragment or endpoint.username:
                raise ValueError
            if notify.scheme not in {'http', 'https'} or not notify.hostname or notify.path != '/payments/notify' or notify.query or notify.fragment or notify.username:
                raise ValueError
            def decode(raw: str) -> bytes:
                return raw.encode() if '-----BEGIN' in raw else base64.b64decode(''.join(raw.split()), validate=True)
            prv, pub = decode(private), decode(public)
            key = serialization.load_pem_private_key(prv, None) if b'-----BEGIN' in prv else serialization.load_der_private_key(prv, None)
            verify = serialization.load_pem_public_key(pub) if b'-----BEGIN' in pub else serialization.load_der_public_key(pub)
            if not isinstance(key, rsa.RSAPrivateKey) or not isinstance(verify, rsa.RSAPublicKey) or min(key.key_size, verify.key_size) < 2048:
                raise ValueError
            timeout = float(os.environ.get('AGENT_OPS_PAYMENT_TIMEOUT', '5'))
            retries = int(os.environ.get('AGENT_OPS_PAYMENT_RETRIES', '2'))
            if not 0 < timeout <= 30 or not 0 <= retries <= 3:
                raise ValueError
            binding = None
            binding_file = folder / 'merchant_binding.json'
            if binding_file.exists() or binding_file.is_symlink():
                record = json.loads(read('merchant_binding.json'))
                if not isinstance(record, dict) or set(record) != {'verified', 'verified_on', 'app_digest', 'seller_digest'} or record['verified'] is not True:
                    raise ValueError
                checked = date.fromisoformat(record['verified_on'])
                if checked.isoformat() != record['verified_on']:
                    raise ValueError
                binding = MerchantBinding(checked, record['app_digest'], record['seller_digest'])
                if not binding.matches(app, seller):
                    raise ValueError
            return cls(app, seller, gateway, notify_url, key, verify, timeout, retries, binding)
        except PaymentError:
            raise
        except (OSError, ValueError, TypeError):
            raise PaymentError('PAYMENT_CONFIGURATION_INVALID') from None

    def verify(self, content: bytes, signature: str) -> None:
        try:
            self.public_key.verify(base64.b64decode(signature, validate=True), content, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError, TypeError):
            raise PaymentError('PAYMENT_SIGNATURE_INVALID') from None

    def notification(self, params: dict[str, str]) -> None:
        if params.get('sign_type') != 'RSA2':
            raise PaymentError('PAYMENT_SIGNATURE_INVALID')
        self.verify(canonical(params, notification=True), params.get('sign', ''))
        if params.get('app_id') != self.app_id:
            raise PaymentError('PAYMENT_APP_MISMATCH')
        if not params.get('seller_id'):
            raise PaymentError('PAYMENT_MERCHANT_UNAVAILABLE')
        if params.get('seller_id') != self.seller_id:
            raise PaymentError('PAYMENT_MERCHANT_MISMATCH')

    def request(self, method: str, business: dict[str, Any]) -> dict[str, str]:
        params = {'app_id': self.app_id, 'method': method, 'charset': 'utf-8', 'sign_type': 'RSA2',
                  'format': 'JSON', 'version': '1.0',
                  'timestamp': datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
                  'biz_content': json.dumps(business, ensure_ascii=False, separators=(',', ':'))}
        return params

    def sign(self, params: dict[str, str]) -> dict[str, str]:
        return {**params, 'sign': base64.b64encode(self.private_key.sign(canonical(params), padding.PKCS1v15(), hashes.SHA256())).decode()}

    def payment_url(self, merchant_no: str, amount: int) -> str:
        params = self.request('alipay.trade.page.pay', {'out_trade_no': merchant_no,
            'total_amount': money(amount), 'subject': 'Sandbox order',
            'product_code': 'FAST_INSTANT_TRADE_PAY', 'timeout_express': '30m'})
        params['notify_url'] = self.notify_url
        return self.gateway + '?' + urlencode(self.sign(params))


@dataclass(repr=False)
class Gateway:
    config: PaymentConfig
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def call(self, method: str, business: dict[str, Any]) -> dict[str, Any]:
        # Stable merchant/refund IDs across all attempts; never invent a new payment after timeout.
        for attempt in range(self.config.retries + 1):
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=self.config.timeout,
                                             transport=self.transport, follow_redirects=False) as client, client.stream('POST', self.config.gateway,
                        data=self.config.sign(self.config.request(method, business))) as response:
                        if response.status_code >= 500:
                            raise PaymentError('PAYMENT_GATEWAY_UNAVAILABLE')
                        if response.status_code != 200:
                            raise PaymentError('PAYMENT_GATEWAY_REJECTED')
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 1024 * 1024:
                                raise PaymentError('PAYMENT_RESPONSE_INVALID')
                encoding = 'utf-8'
                try:
                    raw = data.decode(encoding)
                except UnicodeDecodeError:
                    encoding = 'gb18030'
                    raw = data.decode(encoding)
                name = method.replace('.', '_') + '_response'
                parsed = json.loads(raw)
                # Verify the exact JSON object substring signed by the gateway, without reserialization.
                match = re.search('"' + re.escape(name) + r'"\s*:\s*', raw)
                if match is None or name not in parsed:
                    raise PaymentError('PAYMENT_RESPONSE_INVALID')
                obj, end = json.JSONDecoder().raw_decode(raw[match.end():])
                self.config.verify(raw[match.end():match.end() + end].encode(encoding), parsed.get('sign', ''))
                if not isinstance(obj, dict) or obj.get('code') != '10000':
                    raise PaymentError('PAYMENT_GATEWAY_REJECTED')
                return obj
            except (httpx.TimeoutException, httpx.TransportError):
                error = 'PAYMENT_GATEWAY_UNAVAILABLE'
            except PaymentError as exc:
                if str(exc) != 'PAYMENT_GATEWAY_UNAVAILABLE':
                    raise
                error = str(exc)
            except (ValueError, KeyError, UnicodeError):
                raise PaymentError('PAYMENT_RESPONSE_INVALID') from None
            if attempt < self.config.retries:
                await asyncio.sleep(min(.1 * 2 ** attempt, 1))
        raise PaymentError(error)

    async def query(self, merchant_no: str) -> dict[str, Any]:
        return await self.call('alipay.trade.query', {'out_trade_no': merchant_no})

    async def refund(self, merchant_no: str, refund_no: str, amount: int) -> dict[str, Any]:
        return await self.call('alipay.trade.refund', {'out_trade_no': merchant_no,
            'out_request_no': refund_no, 'refund_amount': money(amount)})

    async def refund_query(self, merchant_no: str, refund_no: str) -> dict[str, Any]:
        return await self.call('alipay.trade.fastpay.refund.query', {'out_trade_no': merchant_no,
                                                                    'out_request_no': refund_no})
