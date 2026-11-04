"""S2 bench：接收本地签名支付通知，验证跨进程只入账一次。"""

from __future__ import annotations

import os
import typing
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request

from agent_ops.payment_gateway import PaymentConfig
from agent_ops.payments import Payments

app = FastAPI()
_provider_public_loaded = serialization.load_pem_public_key(
    Path(os.environ["PAY_PROVIDER_PUB"]).read_bytes()
)
_provider_public = typing.cast(rsa.RSAPublicKey, _provider_public_loaded)
_merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_config = PaymentConfig("bench-app", "bench-seller", "https://gateway.invalid/service",
                        "http://notify.invalid/payments/notify", _merchant,
                        _provider_public, 0.5, 1)
_payments = Payments(os.environ["S1_DATABASE_URL"], _config)


@app.post("/bench/notify")
async def notify(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    await _payments.notify(form)
    return {"status": "ok"}
